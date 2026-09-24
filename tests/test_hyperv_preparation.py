"""The VirtIO preparation a Hyper-V import offers: none, Windows or Linux.

Before this choice existed the wizard had one checkbox, and a Linux guest that was ticked
went through the Windows driver injection, failed it, and was moved onto SATA - where a
guest whose initramfs Hyper-V built finds its disk no better than on VirtIO. Measured on a
CentOS 7.7 guest: it stopped looking for its boot partition. The Linux choice converts the
guest with virt-v2v instead, and these tests hold the pieces of that together: what the
choice means for the hardware, what runs on the node, what happens when it fails, and
how the wizard's default is derived from the disks.
"""

import contextlib
import shlex

import pytest

from pegaprox.core import hyperv, hyperv_linux, hyperv_preflight, hyperv_xhm
from pegaprox.core.hyperv_preflight import OK, WARNING

LINUX_LVM = 'e6d6d379-f507-44c2-a23c-238f2a3df928'
EFI_SYSTEM = 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b'
MS_BASIC_DATA = 'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7'


def _inspection(*gpt_types, mbr_types=(), windows=False):
    partitions = [{'gpt_type': t, 'mbr_type': 0, 'size': 1} for t in gpt_types]
    partitions += [{'gpt_type': '', 'mbr_type': t, 'size': 1} for t in mbr_types]
    return {'inspected': True, 'disks': [{
        'partitions': partitions,
        'volumes': [{'windows': windows, 'file_system': 'NTFS' if windows else ''}]}]}


def _windows_image():
    return [{'windows': True, 'version': '10.0.20348.2582', 'build': 20348,
             'path': 'C:\\vm\\disk.vhdx'}]


# ---------------------------------------------------------------------------
# What the choice builds
# ---------------------------------------------------------------------------

class TestTheChoiceDecidesTheHardware:

    @pytest.mark.parametrize('drivers, controller, nic', [
        ('none', 'sata', 'e1000'),
        ('windows', 'scsi', 'virtio'),
        ('linux', 'scsi', 'virtio'),
    ])
    def test_each_preparation_builds_its_hardware(self, drivers, controller, nic):
        hardware = hyperv_xhm.target_hardware({'drivers': drivers})
        assert (hardware['drivers'], hardware['controller'], hardware['nic_model']) == \
            (drivers, controller, nic)

    def test_a_recorded_migration_keeps_meaning_what_it_meant(self):
        """Records and the injection retry carry `hardware`, from before Linux existed."""
        assert hyperv_xhm.target_hardware({'hardware': 'virtio'})['drivers'] == 'windows'
        assert hyperv_xhm.target_hardware({'hardware': 'compatible'})['drivers'] == 'none'
        assert hyperv_xhm.target_hardware({})['drivers'] == 'none'

    def test_the_choice_wins_over_the_older_field(self):
        config = {'drivers': 'linux', 'hardware': 'virtio'}
        assert hyperv_xhm.target_hardware(config)['drivers'] == 'linux'

    def test_an_unknown_choice_prepares_nothing(self):
        assert hyperv_xhm.target_hardware({'drivers': 'bsd'})['drivers'] == 'none'

    def test_a_named_sata_controller_prepares_nothing(self):
        config = {'drivers': 'linux', 'controller': 'sata'}
        assert hyperv_xhm.target_hardware(config)['controller'] == 'sata'
        assert hyperv_xhm.target_hardware(config)['drivers'] == 'none'


class TestTheDefaultFollowsTheOsType:

    @pytest.mark.parametrize('ostype, drivers', [
        ('win11', 'windows'), ('win10', 'windows'), ('win8', 'windows'),
        ('w2k8', 'windows'), ('wxp', 'windows'),
        ('l26', 'linux'), ('l24', 'linux'),
        ('other', 'none'), ('solaris', 'none'), ('', 'none'), (None, 'none'),
    ])
    def test_each_os_type_has_its_preparation(self, ostype, drivers):
        assert hyperv_xhm.drivers_for_ostype(ostype) == drivers


# ---------------------------------------------------------------------------
# Recognising a Linux guest from the Hyper-V host
# ---------------------------------------------------------------------------

class TestALinuxGuestIsRecognisedByItsPartitions:
    """Windows reads partition types on any disk, even one whose filesystems it cannot open."""

    def test_an_lvm_partition_makes_it_linux(self):
        assert hyperv_xhm.ostype_for([], _inspection(EFI_SYSTEM, LINUX_LVM)) == 'l26'

    def test_an_mbr_linux_partition_makes_it_linux(self):
        assert hyperv_xhm.ostype_for([], _inspection(mbr_types=(0x83,))) == 'l26'

    def test_windows_on_the_image_wins(self):
        assert hyperv_xhm.ostype_for(_windows_image(), _inspection(LINUX_LVM)) == 'win11'

    def test_nothing_recognisable_stays_other(self):
        assert hyperv_xhm.ostype_for([], _inspection(EFI_SYSTEM, MS_BASIC_DATA)) == 'other'
        assert hyperv_xhm.ostype_for([], None) == 'other'

    def test_the_plan_preselects_the_linux_preparation(self):
        detail = {'name': 'shop', 'generation': 2, 'disks': [], 'network_adapters': []}
        defaults = hyperv_xhm.target_defaults(detail, 'guid', 120, [],
                                              _inspection(EFI_SYSTEM, LINUX_LVM))
        assert defaults['ostype'] == 'l26'
        assert defaults['drivers'] == 'linux'

    def test_the_inspection_carries_the_partition_types(self):
        """Braces and case as PowerShell prints a GUID, normalised to one spelling."""
        raw = {'Inspected': True, 'Disks': [{
            'Path': 'C:\\vm\\d.vhdx', 'Volumes': [],
            'Partitions': [{'GptType': '{E6D6D379-F507-44C2-A23C-238F2A3DF928}',
                            'MbrType': 0, 'Size': 1024},
                           {'GptType': '', 'MbrType': 131, 'Size': 2048}]}]}
        disk = hyperv.normalise_disk_inspection(raw)['disks'][0]
        assert disk['partitions'] == [
            {'gpt_type': LINUX_LVM, 'mbr_type': 0, 'size': 1024},
            {'gpt_type': '', 'mbr_type': 0x83, 'size': 2048}]
        assert hyperv_preflight.linux_partitions({'disks': [disk]})

    def test_the_inspection_script_reads_them(self):
        """The script is PowerShell and runs on the host; this pins that it asks."""
        from pegaprox.core import hyperv_scripts
        assert 'Get-Partition' in hyperv_scripts.VM_DISK_INSPECTION
        assert '$entry.Partitions +=' in hyperv_scripts.VM_DISK_INSPECTION


# ---------------------------------------------------------------------------
# The preflight names a choice that does not fit
# ---------------------------------------------------------------------------

class TestThePreflightChecksTheChoice:

    def test_windows_chosen_for_a_linux_disk_is_a_warning(self):
        finding = hyperv_preflight.check_preparation([], _inspection(LINUX_LVM), 'windows')
        assert finding.severity == WARNING and '"Linux"' in finding.detail

    def test_linux_chosen_for_a_windows_disk_is_a_warning(self):
        finding = hyperv_preflight.check_preparation(_windows_image(), None, 'linux')
        assert finding.severity == WARNING and '"Windows"' in finding.detail

    def test_a_fitting_choice_passes(self):
        assert hyperv_preflight.check_preparation(
            [], _inspection(LINUX_LVM), 'linux').severity == OK
        assert hyperv_preflight.check_preparation(
            _windows_image(), None, 'windows').severity == OK

    def test_a_linux_conversion_is_not_a_virtio_vm_without_drivers(self):
        finding = hyperv_preflight.check_virtio_drivers('unknown', 'scsi', False,
                                                        linux_conversion=True)
        assert finding.severity == OK and 'virt-v2v' in finding.summary

    def test_no_windows_is_what_the_linux_choice_expects(self):
        images = [{'windows': False, 'windows_error': 'Get-WindowsImage failed'}]
        assert hyperv_preflight.check_guest_windows(images, False, linux=True).severity == OK
        assert hyperv_preflight.check_guest_windows(images, True).severity == WARNING

    def test_a_dirty_efi_partition_does_not_hold_up_a_linux_guest(self):
        """Measured on the CentOS 7 clone: its FAT EFI partition reports dirty."""
        inspection = {'inspected': True, 'disks': [{'volumes': [
            {'file_system': 'FAT32', 'windows': False, 'dirty': True}]}]}
        assert hyperv_preflight.check_guest_filesystem(inspection, linux=True).severity == OK
        assert hyperv_preflight.check_guest_filesystem(inspection).severity == WARNING

    def test_the_whole_run_reports_it(self):
        report = hyperv_preflight.run_preflight(
            {'state': 'Off', 'generation': 2, 'disks': []}, {},
            {'disk_inspection': _inspection(LINUX_LVM), 'drivers': 'linux',
             'controller': 'scsi', 'drivers_injected': False})
        found = {f.check: f for f in report.findings}
        assert found['preparation'].severity == OK
        assert found['virtio_drivers'].severity == OK


# ---------------------------------------------------------------------------
# What runs on the node, and what a failure leaves
# ---------------------------------------------------------------------------

class _Target:
    pass


class _Task:
    def __init__(self, drivers='linux'):
        self.id = 'mig1'
        self.config = {'drivers': drivers}
        self.target_node = 'node-a'
        self.target_storage = 'vm-pool'
        self.log_lines = []

    def log(self, message):
        self.log_lines.append(str(message))


VOLUMES = [{'index': 1, 'volume': 'vm-pool:vm-120-disk-2', 'controller': 'scsi'},
           {'index': 0, 'volume': 'vm-pool:vm-120-disk-1', 'controller': 'scsi'}]


@pytest.fixture
def node(monkeypatch):
    """A node session that answers from a script and records every command."""
    calls = []
    answers = {'tool': (0, '', ''), 'script': (0, '[ 1.0] Converting\nV2V_EXIT=0\n', '')}

    def run_on_node(_target, _node, command, timeout=600, **_):
        calls.append(command)
        if command == hyperv_linux.TOOL_PROBE:
            return answers['tool']
        if command == hyperv_linux.INSTALL_COMMAND:
            return answers.get('install', (0, 'installed', ''))
        if command.startswith('pvesm path '):
            name = command.split("'")[-2] if "'" in command else command.split()[-1]
            return 0, f'/dev/pve/{name.split(":")[-1]}\n', ''
        return answers['script']

    @contextlib.contextmanager
    def session(_task, _target, min_timeout=0):
        yield run_on_node

    monkeypatch.setattr(hyperv_xhm, '_node_session', session)
    monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers',
                        lambda *a, **k: pytest.fail('the Windows injection ran'))
    return calls, answers


class TestTheLinuxConversionRuns:

    def test_a_converted_guest_is_a_clean_success(self, node):
        calls, _ = node
        task = _Task()
        note = hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {})

        assert note is None
        assert not getattr(task, 'completion_problem', None)
        assert not getattr(task, 'target_unbootable', False)
        script = calls[-1]
        assert 'virt-v2v-in-place --block-driver virtio-scsi' in script
        assert any('guest-exec enabled' in line for line in task.log_lines)

    def test_the_guest_agent_leaves_selinux_confinement_in_the_conversion(self, node):
        """Red as soon as the conversion stops marking virt_qemu_ga_t permissive: an
        enforcing RHEL guest would then arrive with a guest-exec that cannot run `ip`."""
        calls, _ = node
        task = _Task()
        hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {})
        script = calls[-1]
        assert f'--run-command {shlex.quote(hyperv_linux.GUEST_AGENT_SELINUX)}' in script
        assert any('permissive' in line for line in task.log_lines)

    def test_the_disks_go_in_in_their_order(self, node):
        calls, _ = node
        hyperv_xhm._inject_drivers_if_asked(_Task(), _Target(), 120, VOLUMES, {})
        script = calls[-1]
        assert script.index('vm-120-disk-1') < script.index('vm-120-disk-2')
        assert '-i libvirtxml' in script

    def test_a_missing_tool_is_installed_first(self, node):
        calls, answers = node
        answers['tool'] = (1, '', '')
        task = _Task()
        hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {})

        assert calls[1] == hyperv_linux.INSTALL_COMMAND
        assert any('mdadm' in line for line in task.log_lines)

    def test_a_tool_that_cannot_be_installed_leaves_the_vm_unstarted(self, node):
        calls, answers = node
        answers['tool'] = (1, '', '')
        answers['install'] = (100, 'E: Unable to locate package virt-v2v', '')
        task = _Task()
        note = hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {})

        assert 'Unable to locate package' in task.completion_problem
        assert task.target_unbootable
        assert 'not started' in note
        assert not any('virt-v2v-in-place' in call for call in calls[2:])


class TestAFailedConversionIsNotMovedToSata:
    """SATA is no rescue for a guest whose initramfs Hyper-V built."""

    def test_the_vm_stays_on_virtio_and_is_not_started(self, node, monkeypatch):
        _, answers = node
        answers['script'] = (1, 'virt-v2v-in-place: error: no root device\nV2V_EXIT=1\n', '')
        moved = []
        monkeypatch.setattr(hyperv_xhm, '_move_to_compatible_hardware',
                            lambda *a, **k: moved.append(a) or True)
        task = _Task()
        note = hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {})

        assert moved == []
        assert task.target_unbootable
        assert 'exit code 1' in task.completion_problem
        assert 'not started' in note
        assert any('no root device' in line for line in task.log_lines)

    def test_a_node_that_cannot_be_reached_is_the_same_failure(self, monkeypatch):
        @contextlib.contextmanager
        def broken(_task, _target, min_timeout=0):
            raise ConnectionError('no route to node-a')
            yield  # pragma: no cover

        monkeypatch.setattr(hyperv_xhm, '_node_session', broken)
        task = _Task()
        hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {})

        assert task.target_unbootable
        assert 'no route to node-a' in task.completion_problem


class TestAFailedSelinuxStepIsNamed:

    def test_the_vm_is_not_started_and_the_reason_says_selinux(self, node):
        from tests.test_hyperv_linux import SELINUX_FAILURE_OUTPUT
        _, answers = node
        answers['script'] = (1, SELINUX_FAILURE_OUTPUT, '')
        task = _Task()
        note = hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {})

        assert task.target_unbootable
        assert 'SELinux' in task.completion_problem
        assert hyperv_linux.SELINUX_MODULE in task.completion_problem
        assert 'not started' in note
        assert any('semodule' in line for line in task.log_lines)


class TestTheOtherChoicesAreUnchanged:

    def test_none_runs_no_conversion(self, node, monkeypatch):
        calls, _ = node
        monkeypatch.setattr(hyperv_xhm, '_clear_hibernation', lambda *a, **k: None)
        note = hyperv_xhm._inject_drivers_if_asked(_Task('none'), _Target(), 120, VOLUMES,
                                                   {'ostype': 'l26'})
        assert note is None and calls == []

    def test_windows_never_reaches_the_selinux_step(self, monkeypatch):
        """The permissive module is part of the Linux conversion only."""
        commands = []
        monkeypatch.setattr(hyperv_xhm, '_run_offline_injection',
                            lambda *a, **k: (type('View', (), {'no_ntfs_partition': False})(),
                                             True))
        monkeypatch.setattr(hyperv_xhm, '_node_session',
                            lambda *a, **k: commands.append(a) or pytest.fail('node reached'))
        hyperv_xhm._inject_drivers_if_asked(_Task('windows'), _Target(), 120, VOLUMES, {})
        assert commands == []

    def test_windows_runs_the_driver_injection(self, monkeypatch):
        ran = []

        def injection(*_a, **_k):
            ran.append(True)
            return type('View', (), {'no_ntfs_partition': False})(), True

        monkeypatch.setattr(hyperv_xhm, '_run_offline_injection', injection)
        task = _Task('windows')
        assert hyperv_xhm._inject_drivers_if_asked(task, _Target(), 120, VOLUMES, {}) is None
        assert ran == [True]


# ---------------------------------------------------------------------------
# The wizard moves the choice along with the OS type, by the same rule
# ---------------------------------------------------------------------------

OSTYPES = ['win11', 'win10', 'win8', 'win7', 'w2k8', 'wxp', 'wvista', 'l26', 'l24',
           'other', 'solaris', '', 'WIN11']


def test_the_wizard_and_the_server_pair_os_type_and_preparation_alike():
    """Two copies of one rule drift. The wizard's is run in node and compared."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which('node')
    helpers = Path('web/src/hyperv.js')
    if not node or not helpers.exists():
        pytest.skip('node or web/src/hyperv.js is not available')
    script = f'''
const src = require('fs').readFileSync({str(helpers)!r}, 'utf8');
const start = src.indexOf('function hvDriversForOstype');
const end = src.indexOf('\\n        }}\\n', start) + 11;
eval(src.slice(start, end));
console.log(JSON.stringify({json.dumps(OSTYPES)}.map(hvDriversForOstype)));
'''
    done = subprocess.run([node, '-e', script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [hyperv_xhm.drivers_for_ostype(o) for o in OSTYPES]
