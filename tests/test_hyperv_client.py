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

    def add_script(self, script):
        self.script = script

    def invoke(self):
        return self._output


def _client_with(monkeypatch, output, had_errors=False, errors=()):
    client = hc.PsrpHyperVClient(_conn())
    monkeypatch.setattr(client, '_ensure_pool', lambda: object())
    stub_module = type('M', (), {
        'PowerShell': lambda _pool: _StubPowerShell(output, had_errors, errors)
    })
    monkeypatch.setitem(sys.modules, 'pypsrp.powershell', stub_module)
    return client


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
