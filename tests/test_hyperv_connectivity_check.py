# Fork issue #16 (spike of epic #15) — what the read-only probe itself has to get right:
# it must never print a password, a host name or an account name; its embedded PowerShell
# must stay inside what a Hyper-V host can run and must only read; and it must report all
# reachable steps rather than stopping at the first failure.
#
# The failure classification it uses is the product's own and is tested against the module
# in test_hyperv_errors.py. The live connection is proved separately against a released
# test host and written up in docs/hyperv-verification.md.

import importlib.util
import json
import pathlib
import sys

import pytest

# The probe lives in misc/ rather than in the package, so it is loaded by path. It must be
# registered in sys.modules before it runs: @dataclass resolves annotations through
# sys.modules[cls.__module__], which is absent for a module loaded outside the import system.
_MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'misc' / 'hyperv_connectivity_check.py'
_spec = importlib.util.spec_from_file_location('hyperv_connectivity_check', _MODULE_PATH)
probe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


def test_redactor_removes_the_password_from_any_output():
    redact = probe.Redactor()
    redact.add('sup3r-s3cret')
    assert 'sup3r-s3cret' not in redact('auth failed for user with sup3r-s3cret')
    assert probe.REDACTION_PLACEHOLDER in redact('sup3r-s3cret')


def test_redactor_ignores_empty_and_single_character_values():
    redact = probe.Redactor()
    redact.add('')
    redact.add(None)
    redact.add('a')
    assert redact('a plain sentence') == 'a plain sentence'


def test_rendered_output_contains_no_password():
    redact = probe.Redactor()
    redact.add('hunter2')
    results = [
        probe.ProbeResult(name='host_facts', kind=probe.KIND_AUTHENTICATION,
                          detail='Logon failure for user with password hunter2'),
        probe.ProbeResult(name='vm_inventory', kind=probe.KIND_OK,
                          data=[{'name': 'synthetic-vm', 'note': 'hunter2'}]),
    ]
    rendered = probe.render(results, redact)
    assert 'hunter2' not in rendered
    assert 'synthetic-vm' in rendered


def test_normalize_vm_keeps_migration_fields_and_drops_the_rest():
    raw = {
        'Id': '00000000-0000-0000-0000-000000000001',
        'Name': 'synthetic-vm',
        'State': 'Off',
        'Generation': 2,
        'ProcessorCount': 4,
        'MemoryStartup': 4294967296,
        'DynamicMemoryEnabled': True,
        'CheckpointCount': 0,
        'Version': '11.0',
        'Notes': 'internal note that must not travel',
    }
    normalized = probe.normalize_vm(raw)
    assert normalized['id'] == raw['Id']
    assert normalized['generation'] == 2
    assert normalized['dynamic_memory_enabled'] is True
    assert 'Notes' not in json.dumps(normalized)
    assert 'internal note' not in json.dumps(normalized)


def test_normalize_disk_keeps_the_parent_chain_preflight_needs():
    raw = {
        'Path': 'C:\\VMs\\synthetic.avhdx',
        'VhdFormat': 'VHDX',
        'VhdType': 'Differencing',
        'ParentPath': 'C:\\VMs\\synthetic.vhdx',
        'FileSize': 1048576,
        'Size': 42949672960,
        'Attached': True,
    }
    normalized = probe.normalize_disk(raw)
    assert normalized['vhd_type'] == 'Differencing'
    assert normalized['parent_path'] == raw['ParentPath']
    assert normalized['attached'] is True


def test_single_object_from_convertto_json_becomes_a_list():
    assert probe._as_list({'Name': 'one'}) == [{'Name': 'one'}]
    assert probe._as_list([{'Name': 'one'}]) == [{'Name': 'one'}]
    assert probe._as_list(None) == []


def test_probe_turns_a_failure_into_a_classified_result_instead_of_raising():
    def boom():
        raise RuntimeError('Access is denied.')

    result = probe._probe('vm_inventory', boom)
    assert result.kind == probe.KIND_AUTHORIZATION
    assert not result.ok


def test_probe_stops_after_host_facts_fail_so_later_steps_do_not_mask_the_cause(monkeypatch):
    instance = probe.HyperVProbe(host='h', user='u', password='p', port=5986, verify=True)

    def always_refused(_script):
        raise ConnectionError('Connection refused')

    monkeypatch.setattr(instance, '_run_script', always_refused)
    results = instance.run()
    assert [r.name for r in results] == ['host_facts']
    assert results[0].kind == probe.KIND_UNREACHABLE


def test_missing_rights_on_disks_is_reported_next_to_a_working_vm_inventory(monkeypatch):
    instance = probe.HyperVProbe(host='h', user='u', password='p', port=5986, verify=True)

    def by_script(script):
        if 'Win32_OperatingSystem' in script:
            return {'OSCaption': 'Windows Server', 'PSVersion': '5.1.0'}
        if 'Get-VMHardDiskDrive' in script:
            raise RuntimeError('Access is denied.')
        return [{'Id': '1', 'Name': 'synthetic-vm', 'State': 'Off', 'Generation': 2}]

    monkeypatch.setattr(instance, '_run_script', by_script)
    results = {r.name: r for r in instance.run()}
    assert results['host_facts'].ok
    assert results['vm_inventory'].ok
    assert results['disk_inventory'].kind == probe.KIND_AUTHORIZATION


def test_password_is_never_taken_from_argv():
    args = probe.parse_args(['--host', 'h', '--user', 'u'])
    assert not hasattr(args, 'password')
    assert args.port == 5986


def test_missing_password_without_a_tty_fails_loudly(monkeypatch):
    monkeypatch.delenv(probe.PASSWORD_ENV_VAR, raising=False)
    monkeypatch.setattr(probe.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(SystemExit):
        probe.read_password(probe.Redactor())


def test_password_from_the_environment_is_registered_for_redaction(monkeypatch):
    monkeypatch.setenv(probe.PASSWORD_ENV_VAR, 'from-env-secret')
    redact = probe.Redactor()
    assert probe.read_password(redact) == 'from-env-secret'
    assert 'from-env-secret' not in redact('leaking from-env-secret here')


# --- The embedded PowerShell has to run on what a Hyper-V host actually has ---------------
#
# Windows PowerShell 5.1 is the default on Windows Server, and its ConvertTo-Json takes only
# -InputObject, -Depth and -Compress. -AsArray arrived in PowerShell 6 and fails the whole
# call with a parameter error on 5.1, which would make every probe step fail for a reason
# that has nothing to do with the host. These guard that regression.

_POWERSHELL_SCRIPTS = ('_PS_HOST_FACTS', '_PS_VM_INVENTORY', '_PS_DISK_INVENTORY')


@pytest.mark.parametrize('script_name', _POWERSHELL_SCRIPTS)
def test_embedded_powershell_avoids_parameters_absent_from_windows_powershell_51(script_name):
    script = getattr(probe, script_name)
    assert '-AsArray' not in script


@pytest.mark.parametrize('script_name', _POWERSHELL_SCRIPTS)
def test_embedded_powershell_sets_json_depth_explicitly(script_name):
    # The 5.1 default is 2, which truncates without an error and yields a half-read object.
    script = getattr(probe, script_name)
    assert '-Depth' in script


@pytest.mark.parametrize('script_name', _POWERSHELL_SCRIPTS)
def test_embedded_powershell_only_reads(script_name):
    # The probe must never change the host. Anything that mutates is a bug in the script,
    # not a feature to be discovered during a migration.
    script = getattr(probe, script_name)
    forbidden = ('Set-VM', 'Remove-VM', 'Start-VM', 'Stop-VM', 'New-VM',
                 'Remove-VMSnapshot', 'Set-VHD', 'Remove-Item', 'Add-VM')
    for verb in forbidden:
        assert verb not in script


def test_host_facts_does_not_read_the_hosts_own_name():
    # $env:COMPUTERNAME is the one value the probe could learn that the redactor cannot
    # remove, because nobody on this side knows it in advance. This repository is public and
    # host names may not be written down here, so the probe does not ask for it at all.
    assert 'COMPUTERNAME' not in probe._PS_HOST_FACTS
    assert 'ComputerName' not in probe._PS_HOST_FACTS


def test_the_host_and_account_names_are_stripped_from_output():
    redact = probe.Redactor()
    redact.add('probe-host.example')
    redact.add('probe-account')
    rendered = probe.render(
        [probe.ProbeResult(name='host_facts', kind=probe.KIND_AUTHORIZATION,
                           detail="probe-account on probe-host.example: Access is denied.")],
        redact,
    )
    assert 'probe-host.example' not in rendered
    assert 'probe-account' not in rendered
    assert probe.KIND_AUTHORIZATION in rendered


def test_main_registers_the_host_and_account_names_for_redaction(monkeypatch):
    # The test above proves the redactor works once a value is registered. This one proves
    # main() actually registers them: without it, deleting those two lines would leave the
    # suite green while a live run put the host name back into its output.
    monkeypatch.setenv(probe.PASSWORD_ENV_VAR, 'pw')
    registered = []
    original_add = probe.Redactor.add

    def tracking_add(self, secret):
        registered.append(secret)
        original_add(self, secret)

    monkeypatch.setattr(probe.Redactor, 'add', tracking_add)
    monkeypatch.setattr(probe.HyperVProbe, 'run', lambda self: [])
    probe.main(['--host', 'the-host', '--user', 'the-user'])
    assert 'the-host' in registered
    assert 'the-user' in registered


@pytest.mark.parametrize('script_name', ('_PS_VM_INVENTORY', '_PS_DISK_INVENTORY'))
def test_collection_scripts_serialise_through_inputobject_not_the_pipeline(script_name):
    # Measured against a real PowerShell parser: piping @(...) into ConvertTo-Json does NOT
    # produce an array, because the pipeline unrolls it again and a single result comes back
    # as a bare object. Only -InputObject @(...) yields [] / [{...}] / [{...},{...}].
    script = getattr(probe, script_name)
    assert 'ConvertTo-Json -InputObject @($items)' in script
    assert '} | ConvertTo-Json' not in script
    assert '}) | ConvertTo-Json' not in script
