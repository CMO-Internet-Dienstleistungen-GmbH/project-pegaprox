"""_pve_node_exec offered root + the web password to somebody else's hypervisor.

Observed live on a real cluster: one console-tile screenshot produced two
`Authentication (keyboard-interactive) failed.` entries against a node whose own SSH
banner reads "All activities are monitored and logged. Unauthorized access will be
prosecuted to the fullest extent of law." The screendump behind those tiles runs on
every poll, so this was a steady trickle, not a one-off — and a fail2ban response is
IP-wide, which takes :8006 with it and drops the API, the console and SSE together.

Three separate causes, all of them here:
  * the user was hardcoded to 'root' while core/xhm.py has always honoured ssh_user
  * nothing asked whether an SSH credential exists at all, though ssh_diagnose knows
  * the circuit breaker's 'auth failed' pattern matches nothing paramiko ever says,
    so the throttle that should have stopped the repetition never engaged
MK
"""
import pytest

from pegaprox.utils import ssh as ssh_mod


class _Cfg:
    def __init__(self, **kw):
        self.pass_ = kw.get('pass_', 'webpw')
        self.ssh_user = kw.get('ssh_user', '')
        self.ssh_key = kw.get('ssh_key', '')
        self.user = kw.get('user', 'root@pam')


class _Mgr:
    id = 'c1'
    host = '10.0.0.1'
    api_port = 8006

    def __init__(self, diag=None, **cfg):
        self.config = _Cfg(**cfg)
        self._diag = diag
        self.failures = []

    def _is_node_blocked(self, node): return (False, 0)
    def _register_node_failure(self, node): self.failures.append(node)
    def _reset_node_failures(self, node): pass
    def _get_node_ip(self, node): return '10.0.0.7'
    def ssh_diagnose(self, node): return self._diag
    def _api_post(self, *a, **k): raise RuntimeError('no API exec')


@pytest.fixture(autouse=True)
def _no_ip_cache():
    ssh_mod._node_ip_cache.clear()
    yield
    ssh_mod._node_ip_cache.clear()


def test_the_configured_ssh_user_is_used(monkeypatch):
    seen = {}
    monkeypatch.setattr(ssh_mod, '_ssh_exec',
                        lambda host, user, pwd, cmd, **k: seen.update(user=user) or (0, 'ok', ''))
    mgr = _Mgr(ssh_user='pegaprox')
    ssh_mod._pve_node_exec(mgr, 'n1', 'true')
    assert seen['user'] == 'pegaprox', "still hardcoding root, as core/xhm.py never did"


def test_root_remains_the_default_when_none_is_configured(monkeypatch):
    seen = {}
    monkeypatch.setattr(ssh_mod, '_ssh_exec',
                        lambda host, user, pwd, cmd, **k: seen.update(user=user) or (0, 'ok', ''))
    ssh_mod._pve_node_exec(_Mgr(), 'n1', 'true')
    assert seen['user'] == 'root'


def test_a_cluster_with_no_ssh_credential_is_never_offered_to_sshd(monkeypatch):
    """The token-only case (#717): config.pass_ holds the TOKEN SECRET, and handing that
    to sshd is a failed root login on the customer's node for no possible benefit."""
    called = []
    monkeypatch.setattr(ssh_mod, '_ssh_exec',
                        lambda *a, **k: called.append(1) or (0, '', ''))
    mgr = _Mgr(diag=('SSH_NO_CREDENTIALS', 'api token only, no ssh key or password'))
    rc, out, err = ssh_mod._pve_node_exec(mgr, 'n1', 'true')
    assert rc == 1
    assert not called, "attempted SSH anyway"
    assert 'ssh' in err.lower() or 'token' in err.lower()


def test_other_diagnoses_do_not_block_the_attempt(monkeypatch):
    """Only 'no credentials' is a reason not to try. Backoff is handled separately and a
    None diagnosis means nothing known explains it — both must still reach sshd."""
    called = []
    monkeypatch.setattr(ssh_mod, '_ssh_exec',
                        lambda *a, **k: called.append(1) or (0, 'ok', ''))
    mgr = _Mgr(diag=('NODE_BACKOFF', 'in backoff'))
    ssh_mod._pve_node_exec(mgr, 'n1', 'true')
    assert called, "a non-credential diagnosis must not suppress the call"


def test_a_manager_without_the_classifier_still_works(monkeypatch):
    """Older managers predate ssh_diagnose; they must not start raising here."""
    called = []
    monkeypatch.setattr(ssh_mod, '_ssh_exec',
                        lambda *a, **k: called.append(1) or (0, 'ok', ''))
    mgr = _Mgr()
    del mgr.__class__.ssh_diagnose          # simulate the old shape
    try:
        rc, out, err = ssh_mod._pve_node_exec(mgr, 'n1', 'true')
        assert called and rc == 0
    finally:
        _Mgr.ssh_diagnose = lambda self, node: self._diag


# ── the breaker pattern that never matched ──

@pytest.mark.parametrize('err', [
    'Authentication (keyboard-interactive) failed.',
    'Authentication failed.',
    'M1(ki-transport): Authentication (keyboard-interactive) failed.',
    'Permission denied (publickey,password).',
])
def test_an_auth_rejection_trips_the_node_breaker(monkeypatch, err):
    monkeypatch.setattr(ssh_mod, '_ssh_exec', lambda *a, **k: (255, '', err))
    mgr = _Mgr()
    ssh_mod._pve_node_exec(mgr, 'n1', 'true')
    assert mgr.failures == ['n1'], f"breaker sat idle through {err!r} — the repetition is the damage"


@pytest.mark.parametrize('err', [
    "rm: cannot remove '/etc/x': Permission denied",
    "cat: /root/secret: Permission denied",
])
def test_a_command_that_is_merely_denied_does_not_mark_the_node_dead(monkeypatch, err):
    """A bare 'permission denied' is also what a COMMAND says when it fails. Treating that
    as a dead node would take a perfectly healthy hypervisor out of the UI."""
    monkeypatch.setattr(ssh_mod, '_ssh_exec', lambda *a, **k: (1, '', err))
    mgr = _Mgr()
    ssh_mod._pve_node_exec(mgr, 'n1', 'true')
    assert mgr.failures == [], f"{err!r} wrongly marked the node unreachable"
