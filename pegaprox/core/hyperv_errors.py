"""Failure classification for the Hyper-V migration source.

A Hyper-V host refuses a connection for reasons that look alike in a log and are
entirely different jobs to fix: a listener that is not there, a certificate the
client does not trust, a password that is wrong, an account that is not in the
right group, a Hyper-V role that was never installed. Collapsing them into one
"connection failed" moves the work of telling them apart onto whoever reads the
message, usually without the information needed to do it.

So every failure on the way to a host is reduced to one of the kinds below, each
of which names a different place to go and look. The classification is a pure
function of the exception type and its message, which keeps it testable without
a host and keeps the transport layer free of guesswork.
"""

from __future__ import annotations

# Each kind is one operator problem with one place to fix it.
KIND_OK = 'ok'
KIND_UNREACHABLE = 'unreachable'
KIND_TIMEOUT = 'timeout'
KIND_CERTIFICATE = 'certificate'
KIND_AUTHENTICATION = 'authentication'
KIND_AUTHORIZATION = 'authorization'
KIND_MISSING_FEATURE = 'missing_feature'
KIND_CLIENT_DEPENDENCY = 'client_dependency'
# Not a host failure at all: this side declined to send the script. It is a kind rather
# than a plain exception so the refusal reaches an API response with the same shape as
# every other failure, instead of surfacing as an unclassified 500.
KIND_REFUSED = 'refused'
KIND_UNKNOWN = 'unknown'

REMEDIES = {
    KIND_UNREACHABLE: 'Check the WinRM HTTPS listener, its port, and any firewall in between.',
    KIND_TIMEOUT: 'The endpoint accepted the connection but did not answer in time. Check the WSMan '
                  'operation timeout and how long the cmdlet takes on the host.',
    KIND_CERTIFICATE: "Install the host's issuing CA on the PegaProx machine, or correct the "
                      'listener certificate so its subject matches the name used to connect.',
    KIND_AUTHENTICATION: 'Check the account name and password, and that NTLM is an accepted WinRM '
                         'authentication method on the host.',
    KIND_AUTHORIZATION: "Add the account to the host's local 'Hyper-V Administrators' and "
                        "'Remote Management Users' groups.",
    KIND_MISSING_FEATURE: 'Confirm the Hyper-V role and its PowerShell module are installed on the host.',
    KIND_CLIENT_DEPENDENCY: 'Install pypsrp on the PegaProx machine; the request never reached the host.',
    KIND_REFUSED: 'PegaProx declined to send this script because it would change the source. '
                  'Read-only paths may only run read-only cmdlets; this is a defect in the '
                  'caller, not a setting on the host.',
    KIND_UNKNOWN: 'No classification matched. The raw message is carried through unchanged.',
}

# Message fragments, lower-cased. Ordering between the groups matters and is asserted by
# tests; see classify() for why.
_UNREACHABLE_MARKERS = ('name or service not known', 'connection refused', 'no route to host',
                        'network is unreachable', 'nodename nor servname')
_AUTHORIZATION_MARKERS = ('access is denied', 'access denied', '5 (0x5)', 'not authorized', '403')
_AUTHENTICATION_MARKERS = ('401', 'unauthorized', 'logon failure', 'spnego', 'ntlm',
                           'bad username or password', 'authentication failure')
_MISSING_FEATURE_MARKERS = ('is not recognized', 'commandnotfound', 'hyper-v module',
                            'could not be loaded', 'no snap-ins have been registered')


def classify(exc_type_name: str, message: str) -> str:
    """Map one transport or remoting failure onto an operator-actionable kind.

    The exception type decides wherever it can, because type names are stable while
    message wording varies between pypsrp, requests and the Windows side. Messages are
    only consulted as a fallback, and anything unmatched stays KIND_UNKNOWN rather than
    being forced into a neighbouring category: a wrong answer here sends somebody to the
    wrong system, which is worse than no answer.
    """
    lowered = message.lower()
    type_lowered = exc_type_name.lower()

    # A missing client library never left this machine and says nothing about the host.
    if exc_type_name in ('ModuleNotFoundError', 'ImportError'):
        return KIND_CLIENT_DEPENDENCY

    if 'ssl' in type_lowered or 'certificate verify failed' in lowered:
        return KIND_CERTIFICATE
    if 'certificate' in lowered and 'verify' in lowered:
        return KIND_CERTIFICATE

    # A read timeout is not unreachability: the endpoint accepted the connection and then
    # stayed silent, which points at the host's timeouts rather than at the network.
    if exc_type_name == 'ReadTimeout':
        return KIND_TIMEOUT
    if exc_type_name in ('ConnectionError', 'ConnectTimeout', 'NewConnectionError'):
        return KIND_UNREACHABLE
    if any(marker in lowered for marker in _UNREACHABLE_MARKERS):
        return KIND_UNREACHABLE

    if 'authenticationerror' in type_lowered:
        return KIND_AUTHENTICATION

    # WinRM carries authorization faults over HTTP 401, so one message can hold both an
    # "access is denied" and an "unauthorized". The rights marker is the more specific of
    # the two and has to win, or missing group membership reads as a bad password.
    if any(marker in lowered for marker in _AUTHORIZATION_MARKERS):
        return KIND_AUTHORIZATION
    if any(marker in lowered for marker in _AUTHENTICATION_MARKERS):
        return KIND_AUTHENTICATION

    if any(marker in lowered for marker in _MISSING_FEATURE_MARKERS):
        return KIND_MISSING_FEATURE
    return KIND_UNKNOWN


def remedy(kind: str) -> str:
    """The operator-facing next step for a kind, never an empty string for a failure."""
    if kind == KIND_OK:
        return ''
    return REMEDIES.get(kind, REMEDIES[KIND_UNKNOWN])


class HyperVError(Exception):
    """A classified failure on the way to, or on, a Hyper-V host.

    Carries the kind and its remedy alongside the original message so every layer above
    can report the same three things without re-deriving them, and so an API response can
    say what to do rather than only what went wrong.
    """

    def __init__(self, message: str, kind: str = KIND_UNKNOWN, detail: str = ''):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.detail = detail

    @classmethod
    def from_exception(cls, exc: BaseException, context: str = '') -> 'HyperVError':
        """Classify an arbitrary transport exception without losing its text."""
        detail = str(exc)
        kind = classify(type(exc).__name__, detail)
        message = f'{context}: {detail}' if context else detail
        return cls(message, kind=kind, detail=detail)

    @property
    def remedy(self) -> str:
        return remedy(self.kind)

    def to_dict(self) -> dict:
        """The shape the API and the UI render. Never includes credentials."""
        return {'kind': self.kind, 'message': self.message, 'remedy': self.remedy}
