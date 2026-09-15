# Fork issue #15 — the Hyper-V transport. Three things here are load-bearing and none of
# them needs a host to prove:
#
#   The inventory path cannot change a host. A mutating cmdlet on that path is refused,
#   so a stray Remove-VM in a script constant fails a test instead of a customer's VM.
#
#   Nothing that leaves this layer carries a credential. The connection object is printed
#   in tracebacks and log lines, so its repr is part of the contract, not cosmetics.
#
#   The read timeout outlives the operation timeout. Reversed, this side gives up while
#   the host is still working and reports a timeout the host never saw.

import json
import sys
import threading
import time

import pytest

from pegaprox.core import hyperv_client as hc
from pegaprox.core.hyperv_errors import HyperVError, KIND_MISSING_FEATURE

# Assembled rather than written out, so the literal never appears in a file, a log or a
# process list. It stands in for a password; its only job is to be recognisable if it
# escapes into output it should not reach.
FAKE_PASSWORD = 'fixture-' + 'value-' + 'not-a-real-credential'
FAKE_HOST = 'probe-host.example'
FAKE_ACCOUNT = 'probe-account'


def _conn(**overrides):
    """A connection to a host that does not exist, with the secrets a test can look for."""
    return hc.HyperVConnection(
        host=overrides.pop('host', FAKE_HOST),
        username=overrides.pop('username', FAKE_ACCOUNT),
        password=overrides.pop('password', FAKE_PASSWORD),
        **overrides,
    )


class TestConnectionSecrets:
    def test_the_repr_does_not_carry_the_password(self):
        assert FAKE_PASSWORD not in repr(_conn())

    def test_the_repr_still_says_which_host_it_is(self):
        # Useful in a log line; the host name is not a secret to the operator reading it.
        assert FAKE_HOST in repr(_conn())

    def test_an_f_string_of_the_connection_does_not_leak_either(self):
        # The common accident: f'{conn}' in a log call. str() falls back to __repr__.
        assert FAKE_PASSWORD not in f'{_conn()}'
        assert FAKE_PASSWORD not in str(_conn())

    def test_redactions_cover_password_host_and_account(self):
        redactions = _conn().redactions()
        assert FAKE_PASSWORD in redactions
        assert FAKE_HOST in redactions
        assert FAKE_ACCOUNT in redactions

    def test_redact_removes_every_listed_value(self):
        message = f'{FAKE_PASSWORD} failed for {FAKE_ACCOUNT} on {FAKE_HOST}'
        cleaned = hc.redact(message, _conn().redactions())
        assert FAKE_PASSWORD not in cleaned
        assert FAKE_HOST not in cleaned
        assert FAKE_ACCOUNT not in cleaned


class TestTimeouts:
    def test_the_read_timeout_outlives_the_operation_timeout(self):
        conn = _conn(operation_timeout=180)
        assert conn.read_timeout > conn.operation_timeout

    def test_that_holds_for_a_custom_operation_timeout_too(self):
        # A caller raising the operation timeout must not have to remember the other one.
        conn = _conn(operation_timeout=900)
        assert conn.read_timeout > conn.operation_timeout

    def test_the_default_port_is_the_https_listener(self):
        assert _conn().port == 5986


class TestReadOnlyGuard:
    @pytest.mark.parametrize('script', [
        'Remove-VMSnapshot -VMName x',
        'Stop-VM -Name x',
        'Set-VMFirmware -VMName x',
        'New-VM -Name x',
        'Start-VM -Name x',
        'Mount-VMDvdDrive -VMName x',
        'get-vm | remove-vm',
    ])
    def test_a_mutating_cmdlet_is_refused_on_the_read_only_path(self, script):
        with pytest.raises(HyperVError) as caught:
            hc.assert_read_only(script)
        assert caught.value.kind == 'refused'

    @pytest.mark.parametrize('script', [
        'Get-VM | ConvertTo-Json',
        'Get-VHD -Path C:\\x.vhdx',
        'Get-VMNetworkAdapter -VMName x',
        '$os = Get-CimInstance Win32_OperatingSystem',
    ])
    def test_a_reading_cmdlet_passes(self, script):
        hc.assert_read_only(script)

    def test_the_refusal_names_the_verb_it_found(self):
        with pytest.raises(HyperVError) as caught:
            hc.assert_read_only('Get-VM; Remove-VM -Name doomed')
        assert 'Remove-' in caught.value.message


class _StubPowerShell:
    """Stands in for pypsrp's PowerShell so _invoke can be exercised without a host."""

    def __init__(self, output, had_errors=False, errors=()):
        self._output = output
        self.had_errors = had_errors
        self.streams = type('S', (), {'error': list(errors)})()
        self.bound = {}

    def add_script(self, script):
        self.script = script

    def add_parameter(self, name, value):
        self.bound[name] = value

    def invoke(self):
        return self._output


def _client_with(monkeypatch, output, had_errors=False, errors=()):
    client = hc.PsrpHyperVClient(_conn())
    monkeypatch.setattr(client, '_ensure_pool', lambda: object())
    shells = []

    def _make(_pool):
        shell = _StubPowerShell(output, had_errors, errors)
        shells.append(shell)
        return shell

    stub_module = type('M', (), {'PowerShell': _make})
    monkeypatch.setitem(sys.modules, 'pypsrp.powershell', stub_module)
    # The shells this client built, so a test can look at what was bound to them.
    client.stub_shells = shells
    return client


class TestParameterPassing:
    """Values reach the transport as parameters, not as text inside the script.

    The scripts declare [Parameter(Mandatory=$true)], which PowerShell demands when the
    param() block is entered. A value assigned into the body afterwards arrives too late —
    that is what this path used to do, and against a real host every call failed.
    """

    def test_run_json_binds_what_it_was_given(self, monkeypatch):
        client = _client_with(monkeypatch, ['{}'])
        client.run_json('param([string]$VmId)\nGet-VM -Id $VmId', VmId='abc')
        assert client.stub_shells[0].bound == {'VmId': 'abc'}

    def test_run_action_binds_too(self, monkeypatch):
        client = _client_with(monkeypatch, ['{}'])
        client.run_action('param([string]$VmId)\nStop-VM -Id $VmId', 'stop it', VmId='abc')
        assert client.stub_shells[0].bound == {'VmId': 'abc'}

    def test_the_script_reaches_the_transport_unchanged(self, monkeypatch):
        client = _client_with(monkeypatch, ['{}'])
        script = 'param([string]$VmId)\nGet-VM -Id $VmId'
        client.run_json(script, VmId='abc')
        assert client.stub_shells[0].script == script

    def test_a_call_without_parameters_binds_nothing(self, monkeypatch):
        client = _client_with(monkeypatch, ['{}'])
        client.run_json('Get-VMHost')
        assert client.stub_shells[0].bound == {}


class TestInvoke:
    def test_json_split_across_output_records_is_joined_before_parsing(self, monkeypatch):
        # PSRP may fragment one long string; parsing only the first record turns a large
        # inventory into a parse error instead of data.
        payload = json.dumps([{'Name': 'a'}, {'Name': 'b'}])
        half = len(payload) // 2
        client = _client_with(monkeypatch, [payload[:half], payload[half:]])
        assert client.run_json('Get-VM') == [{'Name': 'a'}, {'Name': 'b'}]

    def test_no_output_is_none_rather_than_a_parse_error(self, monkeypatch):
        client = _client_with(monkeypatch, [])
        assert client.run_json('Get-VM') is None

    def test_output_that_is_not_json_raises_with_the_text_redacted(self, monkeypatch):
        client = _client_with(monkeypatch, [f'<html>{FAKE_PASSWORD}</html>'])
        with pytest.raises(HyperVError) as caught:
            client.run_json('Get-VM')
        assert FAKE_PASSWORD not in caught.value.detail
        assert FAKE_PASSWORD not in caught.value.message

    def test_a_host_side_script_error_is_classified_not_swallowed(self, monkeypatch):
        client = _client_with(monkeypatch, [], had_errors=True,
                              errors=["The term 'Get-VM' is not recognized as the name of a cmdlet"])
        with pytest.raises(HyperVError) as caught:
            client.run_json('Get-VM')
        assert caught.value.kind == KIND_MISSING_FEATURE

    def test_a_host_side_error_message_is_redacted(self, monkeypatch):
        client = _client_with(monkeypatch, [], had_errors=True,
                              errors=[f'failed for {FAKE_ACCOUNT} with {FAKE_PASSWORD}'])
        with pytest.raises(HyperVError) as caught:
            client.run_json('Get-VM')
        assert FAKE_PASSWORD not in str(caught.value)
        assert FAKE_ACCOUNT not in str(caught.value)

    def test_run_json_refuses_a_mutating_script_before_any_transport(self, monkeypatch):
        client = hc.PsrpHyperVClient(_conn())

        def fail_if_called():
            raise AssertionError('the transport must not be touched for a refused script')

        monkeypatch.setattr(client, '_ensure_pool', fail_if_called)
        with pytest.raises(HyperVError) as caught:
            client.run_json('Remove-VM -Name doomed')
        assert caught.value.kind == 'refused'

    def test_run_action_allows_what_run_json_refuses(self, monkeypatch):
        # The action path exists precisely so a mutation is a deliberate, named call.
        client = _client_with(monkeypatch, ['null'])
        assert client.run_action('Stop-VM -Name x', 'shut down x') is None


class TestLifecycle:
    def test_close_is_safe_before_anything_was_opened(self):
        hc.PsrpHyperVClient(_conn()).close()

    def test_close_is_idempotent(self):
        client = hc.PsrpHyperVClient(_conn())
        client.close()
        client.close()

    def test_it_works_as_a_context_manager(self):
        with hc.PsrpHyperVClient(_conn()) as client:
            assert isinstance(client, hc.PsrpHyperVClient)

    def test_a_missing_pypsrp_is_reported_as_a_client_problem(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def no_pypsrp(name, *args, **kwargs):
            if name.startswith('pypsrp'):
                raise ImportError("No module named 'pypsrp'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, '__import__', no_pypsrp)
        client = hc.PsrpHyperVClient(_conn())
        with pytest.raises(HyperVError) as caught:
            client.run_json('Get-VM')
        assert caught.value.kind == 'client_dependency'


class _CountingPowerShell(_StubPowerShell):
    """A shell that records how many callers are inside invoke() at the same time."""

    def __init__(self, output, state):
        super().__init__(output)
        self._state = state

    def invoke(self):
        with self._state['lock']:
            self._state['inside'] += 1
            self._state['peak'] = max(self._state['peak'], self._state['inside'])
        # Long enough that a second thread reaches invoke() while this one is in it, if
        # nothing stops it.
        time.sleep(0.05)
        with self._state['lock']:
            self._state['inside'] -= 1
        return self._output


class TestTheSharedPoolIsUsedByOneCallerAtATime:
    """One client is held for the life of the process, so its pool is shared.

    PSRP is a stateful conversation over a single shell. Two callers interleaving on it do
    not fail fast — one waits for a reply the other already took, until the read timeout,
    which is 210 seconds. A UI polling several endpoints at once reaches that on an
    ordinary page load, so the serialisation is what keeps the Hyper-V views answering.
    """

    def test_two_threads_are_never_inside_the_shell_together(self, monkeypatch):
        client = hc.PsrpHyperVClient(_conn())
        monkeypatch.setattr(client, '_ensure_pool', lambda: object())
        state = {'inside': 0, 'peak': 0, 'lock': threading.Lock()}
        stub_module = type('M', (), {'PowerShell': lambda _pool: _CountingPowerShell(['{}'], state)})
        monkeypatch.setitem(sys.modules, 'pypsrp.powershell', stub_module)

        threads = [threading.Thread(target=client.run_json, args=('Get-VM',)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert state['peak'] == 1

    def test_a_transport_failure_throws_the_pool_away(self, monkeypatch):
        # A shell left mid-exchange answers nobody. Keeping it is what turns one failed
        # call into every later call timing out.
        client = hc.PsrpHyperVClient(_conn())
        client._pool = object()
        client._wsman = object()

        def explode(_pool):
            raise OSError('connection reset by peer')

        monkeypatch.setitem(sys.modules, 'pypsrp.powershell', type('M', (), {'PowerShell': explode}))
        with pytest.raises(HyperVError):
            client.run_json('Get-VM')

        assert client._pool is None
        assert client._wsman is None

    def test_a_host_side_script_error_keeps_the_pool(self, monkeypatch):
        # The script failed, the shell did not. Rebuilding the session here would pay for a
        # WSMan handshake on every "no such VM".
        client = _client_with(monkeypatch, [], had_errors=True, errors=['no such VM'])
        client._pool = object()
        with pytest.raises(HyperVError):
            client.run_json('Get-VM')
        assert client._pool is not None


class TestAShellTheHostHasAlreadyClosed:
    """WinRM ends an idle shell on its own schedule, and the caller cannot see that.

    Measured on a registered host: the VM list came back as a broken pipe and the browser
    showed an empty resource table for a host that answered normally a second later. A
    reopened connection is not a second attempt at a failed request — it is the first
    attempt at one that never left.
    """

    def _client(self, monkeypatch, shells):
        client = hc.PsrpHyperVClient(_conn())
        opened = []

        def _ensure():
            if client._pool is None:
                client._pool = object()
                opened.append(1)
            return client._pool

        monkeypatch.setattr(client, '_ensure_pool', _ensure)
        made = iter(shells)
        monkeypatch.setitem(sys.modules, 'pypsrp.powershell',
                            type('M', (), {'PowerShell': lambda _pool: next(made)}))
        client.opened = opened
        return client

    def test_a_stale_pool_is_reopened_and_the_caller_still_gets_an_answer(self, monkeypatch):
        class _Broken:
            def __init__(self):
                self.had_errors = False
            def add_script(self, script): pass
            def add_parameter(self, name, value): pass
            def invoke(self):
                raise OSError('[Errno 32] Broken pipe')

        client = self._client(monkeypatch, [_Broken(), _StubPowerShell(['{"ok": true}'])])
        client._pool = object()  # a pool from an earlier call, which the host has closed
        assert client.run_json('Get-VM') == {'ok': True}
        assert client.opened == [1]  # exactly one reopen, not a loop

    def test_a_host_that_is_really_down_is_reported_without_a_second_wait(self, monkeypatch):
        # Nothing was reused, so there is no stale shell to blame and no reason to pay the
        # timeout twice before saying the host is unreachable.
        class _Broken:
            def __init__(self):
                self.had_errors = False
            def add_script(self, script): pass
            def add_parameter(self, name, value): pass
            def invoke(self):
                raise OSError('connection refused')

        attempts = []

        def _make(_pool):
            attempts.append(1)
            return _Broken()

        client = hc.PsrpHyperVClient(_conn())
        monkeypatch.setattr(client, '_ensure_pool', lambda: object())
        monkeypatch.setitem(sys.modules, 'pypsrp.powershell', type('M', (), {'PowerShell': _make}))
        with pytest.raises(HyperVError):
            client.run_json('Get-VM')
        assert len(attempts) == 1

    def test_a_second_failure_is_reported_rather_than_retried_again(self, monkeypatch):
        class _Broken:
            def __init__(self):
                self.had_errors = False
            def add_script(self, script): pass
            def add_parameter(self, name, value): pass
            def invoke(self):
                raise OSError('[Errno 32] Broken pipe')

        client = self._client(monkeypatch, [_Broken(), _Broken()])
        client._pool = object()
        with pytest.raises(HyperVError):
            client.run_json('Get-VM')

    def test_a_host_side_script_error_is_not_retried(self, monkeypatch):
        # The script failed, the shell did not. Running it twice would repeat whatever the
        # script did before it failed.
        invocations = []

        def _make(_pool):
            invocations.append(1)
            return _StubPowerShell([], had_errors=True, errors=['no such VM'])

        client = hc.PsrpHyperVClient(_conn())
        client._pool = object()
        monkeypatch.setattr(client, '_ensure_pool', lambda: client._pool or object())
        monkeypatch.setitem(sys.modules, 'pypsrp.powershell', type('M', (), {'PowerShell': _make}))
        with pytest.raises(HyperVError):
            client.run_json('Get-VM')
        assert len(invocations) == 1
