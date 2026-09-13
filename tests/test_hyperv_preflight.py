# Fork issue #15 — preflight is the last point at which a migration can be stopped cheaply.
# After it, disks are being read and a target VM exists, so a condition that should have
# blocked the run becomes a half-built guest somebody cleans up by hand.
#
# The rule these tests exist to hold is that an unknown state never passes. Hyper-V can
# answer "I don't know" about a disk, a checkpoint count or a power state, and a preflight
# that reads that as fine is exactly the one that imports a differencing chain and loses
# the data living in it.
#
# Pure functions over normalised data: no host, no network, no mocks.

import pytest

from pegaprox.core import hyperv_preflight as pf


def _vm(**overrides):
    """A VM that passes everything, so each test changes exactly one thing."""
    vm = {
        'state': 'Off',
        'generation': 2,
        'checkpoint_count': 0,
        'disks': [{'path': 'C:\\VMs\\synthetic.vhdx', 'vhd_type': 'Dynamic',
                   'parent_path': None, 'size': 42 * 1024 ** 3}],
        'network_adapters': [{'name': 'Network Adapter', 'mac_address': '00:15:5D:00:00:01'}],
        'secure_boot_enabled': False,
        'vtpm_enabled': False,
        'bitlocker_state': 'off',
        'virtio_driver_state': 'present',
    }
    vm.update(overrides)
    return vm


def _target(**overrides):
    target = {'available_bytes': 500 * 1024 ** 3}
    target.update(overrides)
    return target


def _options(**overrides):
    options = {
        'network_map': {'00:15:5D:00:00:01': 'vmbr0'},
        'controller': 'scsi',
        'reachable_paths': {'C:\\VMs\\synthetic.vhdx': True},
    }
    options.update(overrides)
    return options


class TestUnknownIsNeverFine:
    """Every "I don't know" blocks. This is the class that matters most."""

    def test_an_unknown_power_state_blocks(self):
        assert pf.check_power_state(None).severity == pf.BLOCKING

    def test_an_unknown_checkpoint_count_blocks(self):
        assert pf.check_checkpoints(None).severity == pf.BLOCKING

    def test_an_unknown_disk_type_blocks(self):
        assert pf.check_disk({'path': 'x.vhdx', 'vhd_type': None}).severity == pf.BLOCKING

    def test_unknown_target_capacity_blocks(self):
        assert pf.check_target_capacity(10, None).severity == pf.BLOCKING

    def test_an_unknown_generation_blocks(self):
        assert pf.check_firmware(None).severity == pf.BLOCKING

    def test_unchecked_source_file_access_blocks(self):
        assert pf.check_source_file_access({}).severity == pf.BLOCKING


class TestPowerState:
    def test_a_vm_that_is_off_passes(self):
        assert pf.check_power_state('Off').severity == pf.OK

    @pytest.mark.parametrize('state', ['Saved', 'FastSaved'])
    def test_a_saved_vm_warns_rather_than_passing(self, state):
        # Its disks are not being written to, so this is not a blocker. But the guest's
        # memory is in a saved-state file the disks do not contain: migrating the disks
        # alone throws it away and the guest boots as though it had lost power. Treating
        # that as equivalent to a clean shutdown is the mistake this guards.
        finding = pf.check_power_state(state)
        assert finding.severity == pf.WARNING
        assert 'memory' in finding.detail

    def test_migrating_a_saved_vm_has_to_be_confirmed(self):
        report = pf.run_preflight(_vm(state='Saved'), _target(), _options())
        assert not report.blocked
        assert 'power_state' in report.requires_acknowledgement()

    @pytest.mark.parametrize('state', ['Running', 'Paused', 'Starting', 'Stopping'])
    def test_anything_still_moving_blocks(self, state):
        # A copy taken from a running VM is crash-consistent at best.
        assert pf.check_power_state(state).severity == pf.BLOCKING


class TestCheckpoints:
    def test_no_checkpoints_passes(self):
        assert pf.check_checkpoints(0).severity == pf.OK

    def test_any_checkpoint_blocks(self):
        # The live data is in differencing files; copying the parent alone loses it.
        finding = pf.check_checkpoints(3)
        assert finding.severity == pf.BLOCKING
        assert '3' in finding.summary


class TestDisks:
    @pytest.mark.parametrize('vhd_type', ['Fixed', 'Dynamic'])
    def test_an_importable_disk_passes(self, vhd_type):
        assert pf.check_disk({'path': 'x.vhdx', 'vhd_type': vhd_type}).severity == pf.OK

    def test_a_differencing_disk_blocks(self):
        finding = pf.check_disk({'path': 'x.avhdx', 'vhd_type': 'Differencing',
                                 'parent_path': 'x.vhdx'})
        assert finding.severity == pf.BLOCKING
        assert 'x.vhdx' in finding.detail

    def test_a_disk_with_a_parent_blocks_even_if_its_type_says_otherwise(self):
        # The two signals disagree only when something is wrong; trust the one that would
        # cost data if ignored.
        finding = pf.check_disk({'path': 'x.vhdx', 'vhd_type': 'Dynamic',
                                 'parent_path': 'base.vhdx'})
        assert finding.severity == pf.BLOCKING

    def test_a_pass_through_disk_blocks(self):
        assert pf.check_disk({'path': 'PhysicalDrive2', 'vhd_type': 'Physical'}).severity == pf.BLOCKING

    def test_a_vm_with_no_disks_blocks(self):
        report = pf.run_preflight(_vm(disks=[]), _target(), _options(reachable_paths={'x': True}))
        assert report.blocked


class TestTargetCapacity:
    def test_ample_room_passes(self):
        assert pf.check_target_capacity(10 * 1024 ** 3, 500 * 1024 ** 3).severity == pf.OK

    def test_too_little_room_blocks(self):
        assert pf.check_target_capacity(500 * 1024 ** 3, 10 * 1024 ** 3).severity == pf.BLOCKING

    def test_a_tight_fit_warns_rather_than_blocking(self):
        # It fits, but filling the target during the copy would affect guests already there.
        finding = pf.check_target_capacity(100 * 1024 ** 3, 103 * 1024 ** 3)
        assert finding.severity == pf.WARNING

    def test_the_message_carries_the_numbers_somebody_needs(self):
        finding = pf.check_target_capacity(42 * 1024 ** 3, 12 * 1024 ** 3)
        assert '42.0 GiB' in finding.detail
        assert '12.0 GiB' in finding.detail


class TestNetworkMapping:
    def test_a_fully_mapped_vm_passes(self):
        adapters = [{'name': 'NIC1', 'mac_address': 'AA'}, {'name': 'NIC2', 'mac_address': 'BB'}]
        assert pf.check_network_mapping(adapters, {'AA': 'vmbr0', 'BB': 'vmbr1'}).severity == pf.OK

    def test_an_unmapped_adapter_blocks(self):
        # Guessing puts the VM on a network it may not belong on, and the migration's own
        # result would not show it.
        adapters = [{'name': 'NIC1', 'mac_address': 'AA'}, {'name': 'NIC2', 'mac_address': 'BB'}]
        finding = pf.check_network_mapping(adapters, {'AA': 'vmbr0'})
        assert finding.severity == pf.BLOCKING
        assert 'NIC2' in finding.detail

    def test_a_vm_with_no_adapters_passes(self):
        assert pf.check_network_mapping([], {}).severity == pf.OK

    def test_an_adapter_is_addressed_by_mac_because_names_repeat(self):
        # Hyper-V names every adapter "Network Adapter" by default.
        adapters = [{'name': 'Network Adapter', 'mac_address': 'AA'},
                    {'name': 'Network Adapter', 'mac_address': 'BB'}]
        assert pf.check_network_mapping(adapters, {'AA': 'vmbr0', 'BB': 'vmbr1'}).severity == pf.OK
        assert pf.check_network_mapping(adapters, {'AA': 'vmbr0'}).severity == pf.BLOCKING


class TestFirmware:
    def test_generation_1_maps_to_seabios(self):
        assert pf.GENERATION_FIRMWARE[1] == ('seabios', 'i440fx')

    def test_generation_2_maps_to_ovmf_on_q35(self):
        assert pf.GENERATION_FIRMWARE[2] == ('ovmf', 'q35')

    @pytest.mark.parametrize('generation', [1, 2])
    def test_a_supported_generation_passes(self, generation):
        assert pf.check_firmware(generation).severity == pf.OK

    def test_an_unsupported_generation_blocks(self):
        assert pf.check_firmware(3).severity == pf.BLOCKING


class TestWindowsRisks:
    def test_secure_boot_warns_and_needs_confirming(self):
        finding = pf.check_secure_boot(True, generation=2)
        assert finding.severity == pf.WARNING
        assert finding.check in pf._ACKNOWLEDGEABLE_CHECKS

    def test_secure_boot_on_a_generation_1_vm_is_not_a_thing(self):
        assert pf.check_secure_boot(True, generation=1).severity == pf.OK

    def test_a_vtpm_warns_because_its_state_does_not_travel(self):
        finding = pf.check_vtpm(True)
        assert finding.severity == pf.WARNING
        assert 'not transferred' in finding.detail

    def test_no_vtpm_passes(self):
        assert pf.check_vtpm(False).severity == pf.OK

    def test_unknown_bitlocker_with_a_vtpm_warns(self):
        # Not knowing is the normal case, and with a vTPM present it is worth confirming.
        finding = pf.check_bitlocker(None, vtpm_enabled=True)
        assert finding.severity == pf.WARNING
        assert 'unknown' in finding.summary.lower()

    def test_unknown_bitlocker_without_a_vtpm_is_only_noted(self):
        assert pf.check_bitlocker(None, vtpm_enabled=False).severity == pf.OK

    def test_bitlocker_reported_on_warns(self):
        assert pf.check_bitlocker('on', vtpm_enabled=True).severity == pf.WARNING

    def test_the_bitlocker_finding_never_claims_to_have_looked_inside_the_guest(self):
        detail = pf.check_bitlocker(None, vtpm_enabled=True).detail
        assert 'inside the guest' in detail

    def test_missing_virtio_drivers_warn_when_the_target_needs_them(self):
        finding = pf.check_virtio_drivers(None, controller='virtio')
        assert finding.severity == pf.WARNING
        assert finding.check in pf._ACKNOWLEDGEABLE_CHECKS

    def test_a_sata_target_needs_no_virtio_driver(self):
        # This is the escape hatch: slower, but it boots without preparing the guest.
        assert pf.check_virtio_drivers(None, controller='sata').severity == pf.OK

    def test_drivers_reported_present_pass(self):
        assert pf.check_virtio_drivers('present', controller='virtio').severity == pf.OK


class TestSourceAccess:
    def test_all_paths_readable_passes(self):
        assert pf.check_source_file_access({'a.vhdx': True, 'b.vhdx': True}).severity == pf.OK

    def test_one_unreadable_path_blocks_and_names_it(self):
        finding = pf.check_source_file_access({'a.vhdx': True, 'b.vhdx': False})
        assert finding.severity == pf.BLOCKING
        assert 'b.vhdx' in finding.detail


class TestWholeRun:
    def test_a_clean_vm_is_not_blocked(self):
        report = pf.run_preflight(_vm(), _target(), _options())
        assert not report.blocked, [f.summary for f in report.blocking_findings]

    def test_a_clean_run_still_reports_what_it_checked(self):
        # A silent pass is indistinguishable from a check that never ran.
        report = pf.run_preflight(_vm(), _target(), _options())
        assert len(report.findings) >= 8
        assert all(f.summary for f in report.findings)

    def test_every_blocker_is_reported_not_just_the_first(self):
        # Somebody fixing a migration wants the whole list, not one item per attempt.
        report = pf.run_preflight(
            _vm(state='Running', checkpoint_count=2, generation=9),
            _target(available_bytes=1), _options(network_map={}, reachable_paths={'x': False}))
        checks = {f.check for f in report.blocking_findings}
        assert {'power_state', 'checkpoints', 'firmware', 'target_capacity',
                'network_mapping', 'source_access'} <= checks

    def test_the_report_severity_is_the_worst_finding(self):
        assert pf.run_preflight(_vm(state='Running'), _target(), _options()).severity == pf.BLOCKING
        assert pf.run_preflight(_vm(vtpm_enabled=True), _target(), _options()).severity == pf.WARNING
        assert pf.run_preflight(_vm(), _target(), _options()).severity == pf.OK

    def test_a_warning_does_not_block(self):
        report = pf.run_preflight(_vm(vtpm_enabled=True), _target(), _options())
        assert not report.blocked
        assert report.warnings


class TestTheGate:
    def test_a_clean_report_may_start(self):
        may, reason = pf.may_start(pf.run_preflight(_vm(), _target(), _options()))
        assert may, reason

    def test_a_blocked_report_may_not_start_and_says_why(self):
        report = pf.run_preflight(_vm(state='Running'), _target(), _options())
        may, reason = pf.may_start(report)
        assert not may
        assert 'running' in reason.lower()

    def test_an_unconfirmed_risk_stops_the_start(self):
        report = pf.run_preflight(_vm(vtpm_enabled=True), _target(), _options())
        may, reason = pf.may_start(report, acknowledged=[])
        assert not may
        assert 'vtpm' in reason

    def test_confirming_the_risk_releases_the_start(self):
        report = pf.run_preflight(_vm(vtpm_enabled=True), _target(), _options())
        may, reason = pf.may_start(report, acknowledged=['vtpm', 'bitlocker'])
        assert may, reason

    def test_confirming_a_risk_does_not_release_a_blocker(self):
        # A confirmation is not an override: blocking means it cannot be clicked away.
        report = pf.run_preflight(_vm(state='Running', vtpm_enabled=True), _target(), _options())
        may, _ = pf.may_start(report, acknowledged=['vtpm', 'bitlocker', 'power_state'])
        assert not may

    def test_the_report_lists_exactly_which_confirmations_are_missing(self):
        report = pf.run_preflight(_vm(vtpm_enabled=True, secure_boot_enabled=True),
                                  _target(), _options())
        missing = pf.unacknowledged(report, acknowledged=['vtpm'])
        assert 'secure_boot' in missing
        assert 'vtpm' not in missing


class TestSerialisation:
    def test_a_report_serialises_for_the_api(self):
        data = pf.run_preflight(_vm(vtpm_enabled=True), _target(), _options()).to_dict()
        assert data['severity'] == pf.WARNING
        assert data['blocked'] is False
        assert 'vtpm' in data['requires_acknowledgement']
        assert all({'check', 'severity', 'summary', 'detail'} == set(f) for f in data['findings'])

    def test_an_empty_report_is_not_mistaken_for_a_passed_one(self):
        # Callers must look at findings, not only at severity.
        empty = pf.PreflightReport()
        assert empty.severity == pf.OK
        assert empty.findings == []


class TestTheDeferredTransportProof:
    """"Checked and failed" and "not checked yet" must not collapse into one answer."""

    def test_a_runner_that_probed_nothing_still_blocks(self):
        """The default is strict, so forgetting to probe cannot open the gate.

        Only a caller that knows no transport exists may say so, and it has to say so
        explicitly. A runner about to copy disks never does.
        """
        finding = pf.check_source_file_access({})
        assert finding.severity == pf.BLOCKING

    def test_an_unbuilt_transport_is_a_warning_somebody_has_to_confirm(self):
        finding = pf.check_source_file_access({}, probed=False)
        assert finding.severity == pf.WARNING
        assert 'source_access' in pf._ACKNOWLEDGEABLE_CHECKS

    def test_a_probe_that_found_an_unreadable_file_still_blocks(self):
        # `probed=False` speaks only for an empty result. A caller that hands over findings
        # has probed, whatever it claims, so the flag cannot downgrade a real failure.
        finding = pf.check_source_file_access({'C:\\vm\\a.vhdx': False}, probed=False)
        assert finding.severity == pf.BLOCKING

    def test_the_deferral_cannot_be_acknowledged_away_when_a_blocker_stands(self):
        report = pf.PreflightReport()
        report.add(pf.check_source_file_access({}, probed=False))
        report.add(pf.check_power_state('Running'))

        allowed, reason = pf.may_start(report, acknowledged=['source_access'])
        assert allowed is False
        assert 'blocked' in reason.lower()
