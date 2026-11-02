# Fork issue #15 — turning what a Hyper-V host says into what the rest of PegaProx reads.
#
# The fixtures these run against were produced by executing the product's own PowerShell
# scripts under pwsh with stub cmdlets (tests/hyperv_testbed/). That matters: a hand-written
# fixture only proves that the fixture and the code agree, while these put PowerShell's own
# serialiser in the loop, so property names, array handling and -Depth behaviour are
# exercised for real and only the values are invented.
#
# The rule under test throughout: an unknown value stays unknown. Preflight is built to
# block on uncertainty, and a plausible default substituted here would hand it a confident
# answer instead.

import json
import pathlib

import pytest

from pegaprox.core import hyperv
from pegaprox.core.hyperv_errors import HyperVError
from pegaprox.core.hyperv_client import assert_read_only

FIXTURES = pathlib.Path(__file__).parent / 'hyperv_testbed' / 'ps_fixtures'

GUID_GEN2 = '11111111-1111-1111-1111-111111111111'
GUID_GEN1 = '22222222-2222-2222-2222-222222222222'
GUID_AWKWARD = '33333333-3333-3333-3333-333333333333'


def fixture(name):
    path = FIXTURES / f'{name}.json'
    if not path.exists():
        pytest.skip(f'PowerShell fixture {name} not built; see tests/hyperv_testbed/README.md')
    return json.loads(path.read_text())


class FakeClient:
    """Answers each script with a prepared payload, and records what it was asked.

    Keyed by a fragment of the script text rather than by name, because the manager passes
    the script itself: matching on content proves the manager sent the script it meant to.
    """

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.read_scripts = []
        self.actions = []
        # Every binding the manager asked for, so a test can assert the value reached the
        # transport as a parameter rather than looking for it in the script text.
        self.bindings = []

    def run_json(self, script, **parameters):
        # The real client's guard, not a stand-in: a script the transport would refuse on
        # the read path has to fail here too. Without it the disk inspection, which mounts
        # a VHDX, passed every test and was refused on every real host.
        assert_read_only(script)
        self.read_scripts.append(script)
        self.bindings.append(parameters)
        return self._answer(script)

    def run_action(self, script, description, **parameters):
        self.actions.append((script, description))
        self.bindings.append(parameters)
        return self._answer(script)

    def _answer(self, script):
        for marker, payload in self.answers.items():
            if marker in script:
                return payload
        return None

    def close(self):
        pass


def _manager(answers=None):
    return hyperv.HyperVManager('hyperv-host-a', FakeClient(answers), host='probe-host.example')


class TestInventory:
    def test_a_single_vm_still_arrives_as_a_list(self):
        # The array guarantee, end to end: PowerShell produced this fixture, and one result
        # must not come back as a bare object.
        raw = fixture('vm_inventory_single')
        assert isinstance(raw, list)
        assert len(_manager({'Get-VM': raw}).list_vms()) == 1

    def test_a_host_with_no_vms_is_an_empty_list_not_a_ghost_entry(self):
        assert fixture('vm_inventory_empty') == []
        assert _manager({'Get-VM': []}).list_vms() == []

    def test_every_vm_carries_the_fields_the_ui_lists(self):
        vms = _manager({'Get-VM': fixture('vm_inventory')}).list_vms()
        for vm in vms:
            assert vm['guid']
            assert vm['name']
            assert vm['state']

    def test_memory_is_offered_in_megabytes_as_well_as_bytes(self):
        # Hyper-V answers in bytes; the rest of PegaProx talks megabytes for VM memory.
        vm = _manager({'Get-VM': fixture('vm_inventory_single')}).list_vms()[0]
        assert vm['memory_startup_bytes'] == 4294967296
        assert vm['memory_mb'] == 4096


class TestVmDetail:
    def test_a_clean_generation_2_vm_normalises_completely(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen2')}).get_vm(GUID_GEN2)
        assert vm['generation'] == 2
        assert vm['state'] == 'Off'
        assert vm['checkpoint_count'] == 0
        assert len(vm['disks']) == 1
        assert vm['disks'][0]['vhd_type'] == 'Dynamic'

    def test_a_generation_1_vm_keeps_its_two_disks_in_order(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen1')}).get_vm(GUID_GEN1)
        assert vm['generation'] == 1
        paths = [d['path'] for d in vm['disks']]
        assert paths == ['C:\\VMs\\gen1-os.vhdx', 'C:\\VMs\\gen1-data.vhdx']

    def test_ide_disks_are_pointed_at_sata_rather_than_virtio(self):
        # A guest that booted from IDE has no reason to carry VirtIO drivers, so the
        # suggested target controller must not assume them.
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen1')}).get_vm(GUID_GEN1)
        assert all(d['target_controller_hint'] == 'sata' for d in vm['disks'])

    def test_scsi_disks_are_pointed_at_scsi(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen2')}).get_vm(GUID_GEN2)
        assert vm['disks'][0]['target_controller_hint'] == 'scsi'

    def test_two_nics_keep_their_own_macs_and_vlans(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen1')}).get_vm(GUID_GEN1)
        assert len(vm['network_adapters']) == 2
        macs = {n['mac_address'] for n in vm['network_adapters']}
        assert macs == {'00155D000001', '00155D000002'}
        vlans = {n['vlan_id'] for n in vm['network_adapters']}
        assert 42 in vlans

    def test_a_mac_is_offered_in_the_form_the_target_expects(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen2')}).get_vm(GUID_GEN2)
        nic = vm['network_adapters'][0]
        assert nic['mac_address'] == '00155D000001'
        assert nic['mac_address_colons'] == '00:15:5d:00:00:01'

    def test_checkpoints_are_counted_and_named(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_awkward')}).get_vm(GUID_AWKWARD)
        assert vm['checkpoint_count'] == 2
        assert {c['name'] for c in vm['checkpoints']} == {'Auto', 'Manual'}

    def test_a_vtpm_is_reported(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_awkward')}).get_vm(GUID_AWKWARD)
        assert vm['vtpm_enabled'] is True

    def test_secure_boot_is_a_boolean_not_the_string_hyper_v_sends(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen2')}).get_vm(GUID_GEN2)
        assert vm['secure_boot_enabled'] is True

    def test_a_generation_1_vm_reports_secure_boot_as_off(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen1')}).get_vm(GUID_GEN1)
        assert vm['secure_boot_enabled'] is False

    def test_nothing_returned_for_a_vm_is_an_error_not_an_empty_vm(self):
        with pytest.raises(HyperVError):
            _manager({}).get_vm(GUID_GEN2)


class TestUnknownStaysUnknown:
    """The rule preflight depends on."""

    def test_a_disk_the_host_could_not_read_reports_no_type(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_awkward')}).get_vm(GUID_AWKWARD)
        unreadable = [d for d in vm['disks'] if d['read_error']]
        assert unreadable, 'the awkward fixture should carry an unreadable disk'
        assert unreadable[0]['vhd_type'] is None
        assert unreadable[0]['size'] is None

    def test_an_unreadable_disk_blocks_preflight_rather_than_passing_it(self):
        from pegaprox.core import hyperv_preflight as pf
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_awkward')}).get_vm(GUID_AWKWARD)
        findings = [pf.check_disk(d) for d in vm['disks']]
        assert all(f.severity == pf.BLOCKING for f in findings)

    def test_a_differencing_disk_keeps_its_parent_path(self):
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_awkward')}).get_vm(GUID_AWKWARD)
        differencing = [d for d in vm['disks'] if d['vhd_type'] == 'Differencing']
        assert differencing[0]['parent_path'] == 'C:\\VMs\\awkward.vhdx'

    def test_bitlocker_and_driver_state_are_never_guessed(self):
        # Neither can be seen from outside the guest, and this migration does not enter it.
        vm = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen2')}).get_vm(GUID_GEN2)
        assert vm['bitlocker_state'] is None
        assert vm['virtio_driver_state'] is None

    @pytest.mark.parametrize('value, expected', [
        ('On', True), ('Off', False), ('true', True), ('false', False),
        (None, None), ('Disabled', None), ('', None),
    ])
    def test_an_unrecognised_secure_boot_value_is_unknown_not_false(self, value, expected):
        assert hyperv._secure_boot_enabled(value) is expected


class TestMacFormatting:
    def test_a_hyper_v_mac_becomes_the_colon_form(self):
        assert hyperv.format_mac('00155D000001') == '00:15:5d:00:00:01'

    def test_a_dashed_mac_is_accepted_too(self):
        assert hyperv.format_mac('00-15-5D-00-00-01') == '00:15:5d:00:00:01'

    def test_a_mac_of_the_wrong_length_is_passed_through_unchanged(self):
        # Reshaping it would produce something that looks valid and is not.
        assert hyperv.format_mac('00155D') == '00155D'

    def test_no_mac_is_none(self):
        assert hyperv.format_mac(None) is None
        assert hyperv.format_mac('') is None


class TestMergeDetection:
    """Whether a checkpoint merge has finished, as opposed to having been asked for."""

    def test_plain_disks_with_no_parents_count_as_merged(self):
        assert hyperv.merge_is_complete([
            {'path': 'a.vhdx', 'vhd_type': 'Dynamic', 'parent_path': None},
            {'path': 'b.vhdx', 'vhd_type': 'Fixed', 'parent_path': None}])

    def test_a_remaining_parent_means_not_merged(self):
        assert not hyperv.merge_is_complete([
            {'path': 'a.avhdx', 'vhd_type': 'Differencing', 'parent_path': 'a.vhdx'}])

    def test_a_disk_whose_type_could_not_be_read_counts_as_not_merged(self):
        # Conservative on purpose: the alternative is reading a file Hyper-V is still
        # writing into.
        assert not hyperv.merge_is_complete([
            {'path': 'a.vhdx', 'vhd_type': None, 'parent_path': None}])

    def test_no_disks_at_all_is_not_treated_as_merged(self):
        assert not hyperv.merge_is_complete([])

    def test_a_running_merge_is_visible_in_the_wmi_status(self):
        # The documented signal, and the only one: Remove-VMSnapshot returns before the
        # merge finishes and the checkpoint list is empty by then.
        manager = _manager({'Msvm_ComputerSystem': {
            'PrimaryStatus': 2, 'SecondaryStatus': hyperv.WMI_STATUS_MERGING_DISKS,
            'EnabledState': 3, 'HealthState': 5}})
        state = manager.get_merge_state(GUID_GEN2)
        assert state['merging'] is True
        assert state['busy'] is True

    def test_an_idle_vm_is_neither_merging_nor_busy(self):
        manager = _manager({'Msvm_ComputerSystem': {
            'PrimaryStatus': 2, 'SecondaryStatus': None, 'EnabledState': 3, 'HealthState': 5}})
        state = manager.get_merge_state(GUID_GEN2)
        assert state['merging'] is False
        assert state['busy'] is False

    @pytest.mark.parametrize('status', [
        hyperv.WMI_STATUS_CREATING_SNAPSHOT, hyperv.WMI_STATUS_APPLYING_SNAPSHOT,
        hyperv.WMI_STATUS_DELETING_SNAPSHOT, hyperv.WMI_STATUS_EXPORTING,
        hyperv.WMI_STATUS_MIGRATING,
    ])
    def test_any_background_operation_marks_the_vm_busy(self, status):
        manager = _manager({'Msvm_ComputerSystem': {
            'PrimaryStatus': 2, 'SecondaryStatus': status, 'EnabledState': 3, 'HealthState': 5}})
        assert manager.get_merge_state(GUID_GEN2)['busy'] is True

    def test_a_degraded_vm_is_flagged(self):
        manager = _manager({'Msvm_ComputerSystem': {
            'PrimaryStatus': hyperv.WMI_PRIMARY_DEGRADED, 'SecondaryStatus': None,
            'EnabledState': 3, 'HealthState': 20}})
        assert manager.get_merge_state(GUID_GEN2)['degraded'] is True


class TestTheLastGateBeforeReading:
    """Asked immediately before a disk is opened, not at preflight time."""

    def _manager_with(self, state, checkpoints, secondary=None, primary=2):
        return hyperv.HyperVManager('hyperv-host-a', FakeClient({
            'CheckpointCount': {'Id': GUID_GEN2, 'State': state, 'CheckpointCount': checkpoints},
            'Msvm_ComputerSystem': {'PrimaryStatus': primary, 'SecondaryStatus': secondary,
                                    'EnabledState': 3, 'HealthState': 5},
        }))

    def test_an_off_idle_vm_may_be_read(self):
        may, reason = self._manager_with('Off', 0).disks_are_safe_to_read(GUID_GEN2)
        assert may, reason

    def test_a_vm_started_since_preflight_may_not_be_read(self):
        # The window this closes: somebody starts the VM between approval and transfer.
        may, reason = self._manager_with('Running', 0).disks_are_safe_to_read(GUID_GEN2)
        assert not may
        assert 'Running' in reason

    def test_a_checkpoint_taken_since_preflight_may_not_be_read(self):
        may, reason = self._manager_with('Off', 1).disks_are_safe_to_read(GUID_GEN2)
        assert not may
        assert 'checkpoint' in reason

    def test_a_vm_still_merging_may_not_be_read(self):
        may, reason = self._manager_with(
            'Off', 0, secondary=hyperv.WMI_STATUS_MERGING_DISKS).disks_are_safe_to_read(GUID_GEN2)
        assert not may
        assert 'merging' in reason.lower()

    def test_a_degraded_vm_may_not_be_read(self):
        may, reason = self._manager_with(
            'Off', 0, primary=hyperv.WMI_PRIMARY_DEGRADED).disks_are_safe_to_read(GUID_GEN2)
        assert not may
        assert 'degraded' in reason.lower()


class TestActions:
    def test_a_shutdown_that_the_guest_ignored_is_reported_as_failed(self):
        # The failure this prevents: logging a successful shutdown and then copying the
        # disks of a VM that is still running.
        manager = _manager({'Stop-VM': {'Id': GUID_GEN2, 'State': 'Running',
                                        'ShutdownRequested': True,
                                        'Reason': 'The guest did not shut down within the timeout.'}})
        result = manager.shutdown_vm(GUID_GEN2)
        assert result['succeeded'] is False
        assert result['reason']

    def test_a_shutdown_that_worked_is_reported_as_succeeded(self):
        manager = _manager({'Stop-VM': {'Id': GUID_GEN2, 'State': 'Off',
                                        'ShutdownRequested': True, 'Reason': None}})
        assert manager.shutdown_vm(GUID_GEN2)['succeeded'] is True

    def test_the_shutdown_script_never_forces(self):
        from pegaprox.core import hyperv_scripts as scripts
        assert '-Force' not in scripts.SHUTDOWN_VM
        assert '-TurnOff' not in scripts.SHUTDOWN_VM

    def test_deleting_a_checkpoint_reports_whether_the_merge_finished(self):
        manager = _manager({'Remove-VMSnapshot': {
            'RemovedCount': 1, 'RemainingCheckpoints': 0,
            'Disks': [{'Path': 'a.avhdx', 'VhdType': 'Differencing', 'ParentPath': 'a.vhdx'}]}})
        result = manager.remove_checkpoints(GUID_GEN2, remove_all=True)
        assert result['remaining_checkpoints'] == 0
        assert result['merge_complete'] is False, (
            'an empty checkpoint list is not the merge having finished')

    def test_deleting_a_checkpoint_needs_a_name_or_an_explicit_all(self):
        with pytest.raises(ValueError):
            _manager().remove_checkpoints(GUID_GEN2)

    def test_an_action_is_described_for_the_audit_log(self):
        manager = hyperv.HyperVManager('hyperv-host-a', FakeClient({'Stop-VM': {'State': 'Off'}}))
        manager.shutdown_vm(GUID_GEN2)
        _script, description = manager._client.actions[0]
        assert GUID_GEN2 in description
        assert 'shutdown' in description.lower()


class TestParameterBinding:
    """Caller data must never become PowerShell source.

    It used to be spliced in as an assignment after the param() block, which a real
    PowerShell refuses: a [Parameter(Mandatory)] is demanded on entry, before any
    assignment in the body runs. The values are bound as parameters now, and these tests
    assert that rather than the shape of a generated script.
    """

    def test_a_vm_id_is_bound_as_a_parameter_not_as_text(self):
        manager = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen2')})
        manager.get_vm(GUID_GEN2)
        assert manager._client.bindings[0] == {'VmId': GUID_GEN2}
        # And the script itself is sent unchanged, so its param() block still declares it.
        assert GUID_GEN2 not in manager._client.read_scripts[0]

    def test_the_script_is_sent_exactly_as_written(self):
        from pegaprox.core import hyperv_scripts as scripts
        manager = _manager({'$vm = Get-VM -Id': fixture('vm_detail_gen2')})
        manager.get_vm(GUID_GEN2)
        assert manager._client.read_scripts[0] == scripts.VM_DETAIL

    def test_a_quote_in_a_value_stays_one_value(self):
        # A VM name or ISO path may legally contain a quote. Nothing escapes it any more,
        # because nothing turns it into source: it travels as a parameter.
        awkward = "x'; Remove-VM -Name *"
        manager = _manager({'Set-VMDvdDrive': {'Path': awkward}})
        manager.mount_iso(GUID_GEN2, awkward)
        assert manager._client.bindings[0]['IsoPath'] == awkward

    @pytest.mark.parametrize('name, value', [
        ('TimeoutSeconds', 300), ('All', True), ('CheckpointName', ''),
    ])
    def test_non_string_values_are_passed_through_untouched(self, name, value):
        manager = _manager({'Remove-VMSnapshot': {'RemovedCount': 0}})
        manager.remove_checkpoints(GUID_GEN2, remove_all=True)
        binding = manager._client.bindings[0]
        assert binding['VmId'] == GUID_GEN2
        assert binding['All'] is True
        assert isinstance(binding['CheckpointName'], str)


class TestPropertyVerification:
    def test_a_host_with_every_expected_member_reports_complete(self):
        manager = _manager({'Get-MissingMembers': {
            'InspectedVM': 'synthetic-gen2',
            'Missing': {'VM': None, 'VHD': None, 'NetAdapter': None}}})
        result = manager.verify_properties()
        assert result['complete'] is True
        assert result['missing'] == {}

    def test_missing_members_are_named_rather_than_counted(self):
        # The point is to say which name is absent, because that is the fix.
        manager = _manager({'Get-MissingMembers': {
            'InspectedVM': 'synthetic-gen2',
            'Missing': {'VM': ['Generation', 'DynamicMemoryEnabled'], 'VHD': None}}})
        result = manager.verify_properties()
        assert result['complete'] is False
        assert result['missing']['VM'] == ['Generation', 'DynamicMemoryEnabled']
        assert 'VHD' not in result['missing']

    def test_powershells_null_for_an_empty_array_is_read_as_nothing_missing(self):
        # ConvertTo-Json turns an empty array into null, which must not read as a problem.
        manager = _manager({'Get-MissingMembers': {'InspectedVM': 'x', 'Missing': {'VM': None}}})
        assert manager.verify_properties()['complete'] is True


class TestDiskInspection:
    """The inspection mounts a VHDX read-only, so it has to travel the audited action path."""

    ANSWER = {'State': 'Off', 'Inspected': True, 'Error': '', 'Disks': [{
        'Path': 'C:\\vm\\a.vhdx', 'Mounted': True, 'Error': '', 'AttachedAfter': False,
        'Volumes': [{'FileSystem': 'NTFS', 'Windows': True, 'HiveLoadExit': 0,
                     'ProductName': 'Windows Server 2022 Standard',
                     'CurrentBuildNumber': '20348', 'DirtyExit': 0}]}]}

    def test_the_inspection_is_not_refused_by_the_read_only_guard(self):
        client = FakeClient({'Mount-VHD': self.ANSWER})
        manager = hyperv.HyperVManager('hyperv-host-a', client, host='probe-host.example')

        result = manager.inspect_disks(GUID_GEN2)

        assert result['inspected'] is True
        assert result['disks'][0]['volumes'][0]['product_name'] == 'Windows Server 2022 Standard'
        assert [description for _, description in client.actions], (
            'the inspection was not sent as an audited action')
