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
        # Proxmox validates a VM name as a DNS name, so the fixture carries one it accepts.
        'name': 'synthetic-vm',
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
        # A source that will not come back up on its own. The estate's common value is
        # StartIfRunning, which warns - so a fixture meant to pass everything cannot use it.
        'automatic_start_action': 'Nothing',
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
    def test_generation_1_maps_to_the_machine_proxmox_accepts(self):
        """'i440fx' is what the hardware is called; 'pc' is what the API takes."""
        assert pf.GENERATION_FIRMWARE[1] == ('seabios', 'pc')
        assert 'pc' in pf.check_firmware(1).summary

    def test_the_firmware_check_names_the_same_machine_the_import_builds(self):
        """A check that announces a machine the target refuses describes an import that
        cannot happen."""
        from pegaprox.core.hyperv_xhm import GENERATION_MACHINE
        for generation, (_firmware, machine) in pf.GENERATION_FIRMWARE.items():
            assert machine == GENERATION_MACHINE[generation]

    def test_generation_2_maps_to_ovmf_on_q35(self):
        assert pf.GENERATION_FIRMWARE[2] == ('ovmf', 'q35')

    @pytest.mark.parametrize('generation', [1, 2])
    def test_a_supported_generation_passes(self, generation):
        assert pf.check_firmware(generation).severity == pf.OK

    def test_an_unsupported_generation_blocks(self):
        assert pf.check_firmware(3).severity == pf.BLOCKING


class TestWindowsRisks:
    def test_secure_boot_is_reproduced_rather_than_confirmed_away(self):
        # The import asks for an OVMF variable store with Microsoft's keys already
        # enrolled whenever the source had Secure Boot on, so this is no longer a risk
        # somebody accepts by name - it is a setting that is carried across.
        finding = pf.check_secure_boot(True, generation=2)
        assert finding.severity == pf.OK
        assert finding.check not in pf._ACKNOWLEDGEABLE_CHECKS
        assert 'enrolled' in (finding.detail or '')

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
        missing = pf.unacknowledged(report, acknowledged=[])
        assert 'vtpm' in missing
        assert pf.unacknowledged(report, acknowledged=['vtpm']) == \
            [c for c in missing if c != 'vtpm']
        # Secure Boot is reproduced on the target, so it never asks for a confirmation.
        assert 'secure_boot' not in missing


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


class TestTheDriverWarningDescribesTheImportThatWillHappen:
    """The warning and the hardware have to agree, or the report contradicts itself.

    The default used to be `scsi` while the import built `sata`, so a plan warned that the
    guest needed VirtIO drivers and, two rows below, said the disk was going on SATA. A
    person reading that either installs drivers nobody needed or distrusts the report.
    """

    def _vm(self):
        return {'state': 'Off', 'checkpoint_count': 0, 'generation': 1,
                'disks': [{'path': 'C:\\a.vhdx', 'size': 1, 'vhd_type': 'Dynamic'}],
                'network_adapters': []}

    def test_no_named_controller_means_the_compatible_one(self):
        report = pf.run_preflight(self._vm(), {'available_bytes': 10 ** 12}, {})

        finding = next(f for f in report.findings if f.check == 'virtio_drivers')
        assert finding.severity == pf.OK
        assert 'sata' in finding.summary

    def test_choosing_virtio_brings_the_warning_back(self):
        report = pf.run_preflight(self._vm(), {'available_bytes': 10 ** 12},
                                  {'controller': 'scsi'})

        finding = next(f for f in report.findings if f.check == 'virtio_drivers')
        assert finding.severity == pf.WARNING

    def test_the_plan_and_its_warning_name_the_same_controller(self):
        """Measured against a real Windows guest: the plan said sata, the warning scsi."""
        from pegaprox.core.hyperv_preflight import DEFAULT_TARGET_CONTROLLER
        from pegaprox.core.hyperv_xhm import DEFAULT_CONTROLLER

        assert DEFAULT_TARGET_CONTROLLER == DEFAULT_CONTROLLER


class TestWhetherTheSourceCanComeBackOnItsOwn:
    """The failure this guards against is one machine running twice.

    Measured across a production estate of 159 VMs: 147 StartIfRunning, 12 Nothing, no
    Start at all. So the common case has to be a warning somebody can weigh, not a blocker
    that would stop almost every migration in that estate.
    """

    def test_a_source_that_stays_down_is_fine(self):
        finding = pf.check_automatic_start('Nothing')
        assert finding.severity == pf.OK

    def test_the_common_setting_is_a_warning_to_weigh_not_a_blocker(self):
        finding = pf.check_automatic_start('StartIfRunning')
        assert finding.severity == pf.WARNING
        # It has to say when it fires, or the reader cannot judge the risk.
        assert 'host boots' in finding.detail or 'host boots' in finding.summary

    def test_the_warning_is_one_an_operator_can_accept(self):
        """Blocking it would stop almost every migration in the measured estate."""
        report = pf.run_preflight(_vm(automatic_start_action='StartIfRunning'),
                                  _target(), _options())
        assert 'automatic_start' in report.requires_acknowledgement()
        assert not report.blocked

    def test_an_unconditional_start_says_so_plainly(self):
        finding = pf.check_automatic_start('Start', delay_seconds=30)
        assert finding.severity == pf.WARNING
        assert '30 seconds' in finding.summary

    def test_an_unknown_setting_is_not_reported_as_safe(self):
        """A host that did not answer must not read as 'will not restart'."""
        finding = pf.check_automatic_start(None)
        assert finding.severity == pf.WARNING
        assert finding.severity != pf.OK

    def test_the_check_reaches_the_report(self):
        vm = {'state': 'Off', 'checkpoint_count': 0, 'generation': 2, 'disks': [],
              'network_adapters': [], 'automatic_start_action': 'StartIfRunning'}
        report = pf.run_preflight(vm, {'available_bytes': 10 ** 12}, {})
        assert any(f.check == 'automatic_start' for f in report.findings)


# ---------------------------------------------------------------------------
# What the shutdown script may and may not do.
#
# These read the script text, not PowerShell, so they run everywhere - unlike the
# parameter-binding tests, which need pwsh and are skipped without it. That matters here:
# the rule they protect is a safety rule, and a safety rule that only gets checked on a
# machine with PowerShell installed is not checked.
# ---------------------------------------------------------------------------

def test_the_shutdown_never_forces():
    """Forcing is an operator's decision, not a default.

    Proxmox puts it behind its own "Force Stop" entry for the same reason: on a migration
    source it leaves the disks as an unexpected power cut would.
    """
    from pegaprox.core import hyperv_scripts

    assert '-Force' not in hyperv_scripts.SHUTDOWN_VM
    assert '-TurnOff' not in hyperv_scripts.SHUTDOWN_VM


def test_the_shutdown_checks_the_service_before_asking():
    """Hyper-V prompts when the integration service does not answer, and a non-interactive
    session cannot answer - the call then fails with a PowerShell message about user
    interaction and nothing is shut down. Checking first turns that into a sentence naming
    the cause."""
    from pegaprox.core import hyperv_scripts

    assert 'ShutdownComponent' in hyperv_scripts.SHUTDOWN_VM
    assert 'PrimaryStatusDescription' in hyperv_scripts.SHUTDOWN_VM


def test_the_service_is_found_by_type_not_by_name():
    """On a German host it is called 'Herunterfahren'. Matching the name would report every
    guest on such a host as unable to shut down."""
    from pegaprox.core import hyperv_scripts

    assert "-Name 'Shutdown'" not in hyperv_scripts.SHUTDOWN_VM
    assert "GetType().Name -eq 'ShutdownComponent'" in hyperv_scripts.SHUTDOWN_VM


class TestWhetherTheGuestCanBeShutDownAtAll:
    """Found on a real guest: Hyper-V prompts for confirmation when the shutdown service
    does not answer, a non-interactive session cannot answer, and nothing shuts down. That
    has to be known before a migration window is scheduled, not during it."""

    def test_a_guest_that_answers_is_fine(self):
        assert pf.check_orderly_shutdown(True, 'Running').severity == pf.OK

    def test_a_guest_that_cannot_be_asked_is_a_warning(self):
        finding = pf.check_orderly_shutdown(False, 'Running')
        assert finding.severity == pf.WARNING
        # It must say what to do instead, and that forcing is not the answer.
        assert 'from inside' in finding.detail
        assert 'forced off' in finding.detail

    def test_not_knowing_is_not_the_same_as_knowing_it_works(self):
        assert pf.check_orderly_shutdown(None, 'Running').severity == pf.WARNING

    def test_a_vm_that_is_already_off_needs_nobody_to_shut_it_down(self):
        assert pf.check_orderly_shutdown(False, 'Off').severity == pf.OK

    def test_the_operator_can_accept_it_and_shut_the_guest_down_themselves(self):
        report = pf.run_preflight(_vm(state='Off'), _target(), _options())
        assert not report.blocked


def test_the_driver_risk_stays_for_a_caller_that_does_not_inject():
    """The warning is not deleted, it is answered by what the migration does.

    A caller that puts a guest on a VirtIO controller without writing the drivers in is
    still building a VM that cannot find its disk, and the check says so.
    """
    finding = pf.check_virtio_drivers(None, 'scsi', drivers_injected=False)
    assert finding.severity == pf.WARNING
    assert 'will not boot' in finding.detail


def test_the_driver_risk_is_answered_when_the_migration_installs_them():
    finding = pf.check_virtio_drivers(None, 'scsi', drivers_injected=True)
    assert finding.severity == pf.OK
    #: The two cases the injection cannot cover are named rather than left out, because
    #: both end with the VM on a different controller than the operator asked for.
    assert 'no signature the loader accepts' in finding.detail
    assert 'not Windows' in finding.detail

class TestTheVlanFinding:
    """A VLAN is shown before the run, because an adapter on the wrong one looks healthy."""

    def _nic(self, **kw):
        base = {'name': 'Network Adapter', 'mac_address': '00155D000001',
                'switch_name': 'External'}
        base.update(kw)
        return base

    def test_an_adapter_with_a_source_vlan_is_reported_as_tagged(self):
        finding = pf.check_vlan_mapping(
            [self._nic(vlan_mode='Access', vlan_id=22)], {})
        assert finding.severity == 'ok'
        assert '22' in finding.detail

    def test_a_trunk_adapter_warns_and_names_itself(self):
        """It carries several ids, so none of them can be carried over."""
        finding = pf.check_vlan_mapping(
            [self._nic(name='LAN', vlan_mode='Trunk')], {})
        assert finding.severity == 'warning'
        assert 'LAN' in finding.detail
        assert 'Trunk' in finding.detail

    def test_an_adapter_that_would_arrive_untagged_warns(self):
        """Untagged is not neutral: it lands on the target bridge's native VLAN."""
        finding = pf.check_vlan_mapping(
            [self._nic(name='LAN')], {'00155D000001': 0})
        assert finding.severity == 'warning'
        assert 'LAN' in finding.detail

    def test_the_operator_choice_is_what_the_report_answers_for(self):
        """The report has to describe the migration the wizard would start, not the source."""
        finding = pf.check_vlan_mapping(
            [self._nic(vlan_mode='Access', vlan_id=22)], {'00155D000001': 99})
        assert finding.severity == 'ok'
        assert '99' in finding.detail

    def test_a_vm_without_adapters_is_not_a_finding(self):
        assert pf.check_vlan_mapping([], {}).severity == 'ok'


class TestTheHostWideTransportCheck:
    """Whether a target node can read the host is a host fact, measured once.

    Before this, the preflight asked it per VM, could not answer it, and produced a
    warning to confirm per VM. On 159 guests that is 159 confirmations of one question,
    and the twenty-first says nothing the first did not.
    """

    def test_a_successful_check_replaces_the_confirmation_with_a_dated_fact(self):
        finding = pf.check_source_file_access(
            {}, probed=False,
            host_check={'ok': True, 'at_text': '2026-09-15 20:40', 'node': 'pve-1',
                        'shares': ['VMS$']})
        assert finding.severity == pf.OK
        assert '2026-09-15 20:40' in finding.summary
        assert 'pve-1' in finding.detail and 'VMS$' in finding.detail

    def test_a_failed_check_is_a_blocker_not_a_risk_to_accept(self):
        # A host nothing can read is a host nothing can be migrated from. Offering that as
        # a checkbox would let somebody tick their way to a run that cannot move a byte.
        finding = pf.check_source_file_access(
            {}, probed=False,
            host_check={'ok': False, 'at_text': 'just now', 'node': 'pve-1',
                        'error': 'this node has no CIFS support.'})
        assert finding.severity == pf.BLOCKING
        assert 'CIFS' in finding.detail

    def test_without_a_check_it_is_still_the_unknown_to_confirm(self):
        finding = pf.check_source_file_access({}, probed=False, host_check=None)
        assert finding.severity == pf.WARNING
        assert finding.check in pf._ACKNOWLEDGEABLE_CHECKS

    def test_a_real_probe_still_decides(self):
        # The host check says the share is reachable; this VM's own file is not. The
        # per-disk probe is the one that runs on the way to an irreversible copy.
        finding = pf.check_source_file_access(
            {'C:\\vm\\a.vhdx': False},
            host_check={'ok': True, 'at_text': 'earlier', 'node': 'pve-1'})
        assert finding.severity == pf.BLOCKING

    def test_a_successful_check_does_not_hide_a_missing_probe_in_a_run(self):
        # `probed` defaults to True for a runner, and an empty result then still blocks
        # however good the host check was.
        finding = pf.check_source_file_access({}, host_check={'ok': True, 'at_text': 'x'})
        assert finding.severity == pf.BLOCKING


class TestAnUnreportedSecureBootState:
    """None is not False, and the difference decides whether a guest boots.

    Hyper-V returns nothing for Secure Boot on hosts that do not expose the property, and
    the import does not enrol keys under a guest it cannot read. Reporting that as "not
    enabled on this VM" would hide the one case where the operator has to act.
    """

    def test_it_is_a_warning_not_a_silent_ok(self):
        finding = pf.check_secure_boot(None, generation=2)
        assert finding.severity == pf.WARNING
        assert 'did not report' in finding.summary

    def test_off_is_still_a_plain_ok(self):
        assert pf.check_secure_boot(False, generation=2).severity == pf.OK

    def test_on_is_still_reproduced(self):
        finding = pf.check_secure_boot(True, generation=2)
        assert finding.severity == pf.OK
        assert 'enrolled' in finding.detail


# ===========================================================================
# The address the rest of the network knows the guest by
# ===========================================================================

class TestTheMacSurvivesTheMigration:
    """A guest that comes up with a different MAC is a different machine to the network.

    DHCP reservations, static leases, port security, licence bindings and firewall rules
    are written against it. Nothing about a migration's own result shows that they stopped
    matching, which is why this is a question asked before the transfer rather than a
    surprise after it.
    """

    def test_an_assigned_dynamic_mac_is_not_a_problem(self):
        """Dynamic means Hyper-V chose it, not that it is temporary."""
        adapters = [{'name': 'Network Adapter', 'mac_address': '00155D000001',
                     'dynamic_mac': True}]

        finding = pf.check_mac_addresses(adapters)

        assert finding.severity == pf.OK

    def test_an_adapter_that_never_had_one_is_named_and_confirmed(self):
        adapters = [{'name': 'Network Adapter', 'mac_address': '00155D000001'},
                    {'name': 'Network Adapter', 'mac_address': '000000000000'}]

        finding = pf.check_mac_addresses(adapters)

        assert finding.severity == pf.WARNING
        assert '#2' in finding.detail, 'the operator cannot tell which adapter it means'
        assert '#1' not in finding.detail, 'the adapter that has an address is not at risk'
        assert 'mac_addresses' in pf._ACKNOWLEDGEABLE_CHECKS

    def test_a_vm_without_adapters_is_not_warned_about(self):
        assert pf.check_mac_addresses([]).severity == pf.OK


class TestTheNameTheTargetWillAccept:
    """Proxmox validates a VM name as a DNS name, and says so only when it creates the VM.

    That call happens after the disks have been converted. A 100 GiB copy that ran for a
    minute and a half was thrown away because the source VM was called `TestMig_CLONE`, so
    the question has to be asked while it is still cheap to answer.
    """

    def test_an_underscore_is_not_a_dns_name(self):
        assert not pf.is_valid_pve_name('TestMig_CLONE')
        assert not pf.is_valid_pve_name('has space')
        assert not pf.is_valid_pve_name('-leading')
        assert not pf.is_valid_pve_name('trailing-')
        assert not pf.is_valid_pve_name('')

    def test_what_proxmox_does_accept(self):
        assert pf.is_valid_pve_name('TestMig-CLONE')
        assert pf.is_valid_pve_name('srv01')
        assert pf.is_valid_pve_name('srv01.example.test')

    def test_the_suggestion_keeps_the_name_recognisable(self):
        assert pf.pve_name_for('TestMig_CLONE', 'fallback') == 'TestMig-CLONE'
        assert pf.pve_name_for('DC 01 (alt)', 'fallback') == 'DC-01--alt'

    def test_a_name_with_nothing_left_falls_back(self):
        assert pf.pve_name_for('___', 'hyperv-42') == 'hyperv-42'
        assert pf.pve_name_for(None, 'hyperv-42') == 'hyperv-42'

    def test_a_source_name_the_target_refuses_warns_and_names_the_replacement(self):
        finding = pf.check_target_name('TestMig_CLONE')
        assert finding.severity == pf.WARNING
        assert 'TestMig-CLONE' in finding.summary

    def test_a_name_the_operator_typed_blocks_instead_of_being_corrected(self):
        finding = pf.check_target_name('TestMig_CLONE', 'still_wrong')
        assert finding.severity == pf.BLOCKING
        assert 'still_wrong' in finding.summary

    def test_a_chosen_name_that_works_passes(self):
        finding = pf.check_target_name('TestMig_CLONE', 'TestMig-CLONE')
        assert finding.severity == pf.OK

    def test_the_whole_run_blocks_on_a_name_that_cannot_be_created(self):
        report = pf.run_preflight(_vm(name='TestMig_CLONE'), _target(),
                                  dict(_options(), target_name='no_good'))
        assert report.blocked
        allowed, why = pf.may_start(report, [])
        assert not allowed and 'no_good' in why


def _image(**overrides):
    """One disk as `Get-WindowsImage` and `Get-VHD` describe it, normalised."""
    image = {'path': 'S:\\vm\\a.vhdx', 'windows': True, 'build': 20348,
             'version': '10.0.20348.2582', 'architecture': 'x64', 'edition_id': 'ServerStandard',
             'installation_type': 'Server', 'registry_readable': True, 'attached': False,
             'vhd_type': 'Dynamic', 'parent_path': '', 'system_root': 'Windows',
             'windows_error': ''}
    image.update(overrides)
    return image


def _inspection(**volume_overrides):
    volume = {'file_system': 'NTFS', 'size': 99 * 1024 ** 3, 'free': 80 * 1024 ** 3,
              'windows': True, 'hibernated': False, 'hiberfil_size': 0,
              'page_file': True, 'dirty': False}
    volume.update(volume_overrides)
    return {'inspected': True, 'error': '', 'state': 'Off',
            'disks': [{'path': 'S:\\vm\\a.vhdx', 'mounted': True, 'error': '',
                       'attached_after': False, 'volumes': [volume]}]}


class TestWhatTheDisksSayAboutTheGuest:
    """Read on the Hyper-V host, off a stopped VM. The integration services report the
    guest's version over KVP and those items exist only while it runs — measured: complete
    while running, empty three seconds after the guest finished shutting down. A migration
    needs the VM off, so the disk is the only source left."""

    def test_the_windows_version_is_reported(self):
        finding = pf.check_guest_windows([_image()])
        assert finding.severity == pf.OK
        assert '10.0.20348.2582' in finding.summary

    def test_a_disk_with_no_windows_is_not_called_linux(self):
        finding = pf.check_guest_windows([_image(windows=False, windows_error='no image')])
        assert finding.severity == pf.WARNING
        assert 'Linux' in finding.detail and 'not distinguished' in finding.detail

    def test_nothing_read_matters_only_where_it_decides_something(self):
        assert pf.check_guest_windows([], injecting=False).severity == pf.OK
        assert pf.check_guest_windows([], injecting=True).severity == pf.WARNING

    def test_the_windows_disk_is_the_one_carrying_windows(self):
        images = [_image(path='data.vhdx', windows=False), _image(path='system.vhdx')]
        assert pf.windows_disk(images)['path'] == 'system.vhdx'

    def test_a_registry_that_cannot_be_read_warns_before_the_copy(self):
        # The version comes from the image header, the edition from the guest's SOFTWARE
        # hive. One without the other means the hive could not be read — and that is the
        # hive the driver injection edits, where it fails after the disks are converted.
        finding = pf.check_guest_registry([_image(registry_readable=False)])
        assert finding.severity == pf.WARNING
        assert 'SOFTWARE hive' in finding.detail

    def test_a_disk_attached_elsewhere_blocks(self):
        finding = pf.check_disk_in_use([_image(attached=True)])
        assert finding.severity == pf.BLOCKING

    def test_a_32_bit_guest_warns_about_the_driver_variant(self):
        finding = pf.check_guest_architecture([_image(architecture='x86')])
        assert finding.severity == pf.WARNING
        assert 'amd64' in finding.summary


class TestTheDriverReleaseIsDecidedBeforeTheCopy:
    """The same rule runs again during the injection — but by then the disks have been
    converted, and a refusal there has already cost the copy."""

    def test_server_2012_r2_with_the_right_iso_passes(self):
        finding = pf.check_driver_release([_image(build=9600, version='6.3.9600.1')],
                                          'vm-pool:iso/virtio-win-0.1.189.iso')
        assert finding.severity == pf.OK

    def test_server_2012_r2_with_a_newer_iso_blocks(self):
        finding = pf.check_driver_release([_image(build=9600, version='6.3.9600.1')],
                                          'vm-pool:iso/virtio-win-0.1.302.iso')
        assert finding.severity == pf.BLOCKING
        assert '0.1.189' in finding.detail

    def test_a_current_guest_with_the_legacy_iso_blocks(self):
        # 0.1.189 has no 2k22, w11 or 2k25 directory at all.
        finding = pf.check_driver_release([_image(build=20348)],
                                          'vm-pool:iso/virtio-win-0.1.189.iso')
        assert finding.severity == pf.BLOCKING

    def test_no_iso_for_a_guest_that_needs_a_specific_one_blocks(self):
        finding = pf.check_driver_release([_image(build=9600)], '')
        assert finding.severity == pf.BLOCKING
        assert '0.1.189' in finding.summary

    def test_no_injection_means_no_release_has_to_match(self):
        finding = pf.check_driver_release([_image(build=9600)], '', injecting=False)
        assert finding.severity == pf.OK


class TestWhatIsInsideTheDisks:
    """A hibernated guest and an unclean file system are invisible from outside the disk,
    and both turn into a migration that fails after the copy."""

    def test_a_hibernated_guest_is_reported_with_its_size(self):
        finding = pf.check_guest_hibernated(
            _inspection(hibernated=True, hiberfil_size=6 * 1024 ** 3))
        assert finding.severity == pf.WARNING
        assert '6.0 GiB' in finding.summary
        assert 'cannot be resumed on the target' in finding.detail

    def test_a_clean_shutdown_passes(self):
        assert pf.check_guest_hibernated(_inspection()).severity == pf.OK

    def test_a_dirty_volume_warns(self):
        finding = pf.check_guest_filesystem(_inspection(dirty=True))
        assert finding.severity == pf.WARNING
        assert 'transaction logs' in finding.detail

    def test_an_unanswered_dirty_check_is_not_a_clean_one(self):
        finding = pf.check_guest_filesystem(_inspection(dirty=None))
        assert finding.severity == pf.WARNING
        assert 'could not be determined' in finding.summary

    def test_a_disk_the_host_did_not_release_blocks(self):
        inspection = _inspection()
        inspection['disks'][0]['attached_after'] = True
        finding = pf.check_inspection_released_the_disks(inspection)
        assert finding.severity == pf.BLOCKING
        assert 'cannot start' in finding.detail

    def test_hibernation_has_to_be_confirmed_rather_than_clicked_past(self):
        report = pf.run_preflight(
            _vm(), _target(),
            dict(_options(), drivers_injected=True,
                 guest_images=[_image()],
                 virtio_iso='vm-pool:iso/virtio-win-0.1.302.iso',
                 disk_inspection=_inspection(hibernated=True, hiberfil_size=1024 ** 3)))
        assert 'guest_hibernated' in report.requires_acknowledgement()
        allowed, why = pf.may_start(report, [])
        assert not allowed and 'guest_hibernated' in why
        assert pf.may_start(report, ['guest_hibernated'])[0]
