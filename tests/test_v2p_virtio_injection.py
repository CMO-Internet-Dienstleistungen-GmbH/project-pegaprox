"""What `_inject_virtio_drivers` asks a Proxmox node to do.

The injection is one long shell script handed to the node, so the parts of it that
can be wrong without failing loudly are pinned here as strings: which packages apt
is asked for, and where the driver catalogues are written. Both were measured wrong
on a real PVE 9.2 node before these tests existed.
"""
import re

import pytest

import pegaprox.core.v2p as v2p
from pegaprox.core.virtio_firstboot import FIRST_BOOT_SCRIPT, FIRST_BOOT_SERVICE_COMMAND


class _Task:
    """The attributes the injection reads, and a log that keeps its lines."""

    def __init__(self):
        self.install_virtio_drivers = True
        self.target_node = 'node1'
        self.proxmox_vmid = 100
        self.target_storage = 'local-lvm'
        self.virtio_iso_path = '/var/lib/vz/template/iso/virtio-win.iso'
        self.lines = []

    def log(self, message):
        self.lines.append(str(message))


class _Manager:
    """Enough of a PegaProxManager that the running-VM guard can run."""

    host = '127.0.0.1'
    api_port = 8006

    def _api_get(self, url):
        raise RuntimeError('no API in this test')


@pytest.fixture
def node_calls(monkeypatch):
    """Record every command the injection sends, and answer each one from a script."""
    calls = []
    answers = {}

    def fake_exec(pve_mgr, node, cmd, timeout=600, **kwargs):
        calls.append(cmd)
        for needle, reply in answers.items():
            if needle in cmd:
                return reply
        return 0, '', ''

    monkeypatch.setattr(v2p, '_pve_node_exec', fake_exec)
    return calls, answers


def _apt_command(calls):
    return next(cmd for cmd in calls if 'apt-get install' in cmd)


def test_the_node_is_never_asked_for_qemu_utils(node_calls):
    # qemu-utils conflicts with pve-qemu-kvm: apt offers to remove proxmox-ve and
    # pve-apt-hook then aborts the run, so nothing else in the list installs either.
    calls, answers = node_calls
    answers["import hivex"] = (1, '', '')
    answers['apt-get install'] = (1, '', '')

    assert v2p._inject_virtio_drivers(_Manager(), _Task()) is False
    assert 'qemu-utils' not in _apt_command(calls)


def test_the_tool_probe_does_not_wait_for_qemu_nbd(node_calls):
    # On PVE qemu-nbd comes with pve-qemu-kvm. Probing for it would send a node that
    # has every tool the injection can install into an apt run that cannot help.
    calls, answers = node_calls
    answers["import hivex"] = (1, '', '')
    answers['apt-get install'] = (1, '', '')

    v2p._inject_virtio_drivers(_Manager(), _Task())
    probe = calls[0]
    assert 'qemu-nbd' not in probe


def test_the_hivex_shell_the_version_probe_uses_is_installed(node_calls):
    # Without hivexsh the script reads no ProductName and no CurrentBuildNumber, the
    # build table matches nothing, and every guest silently gets the w11/amd64 drivers.
    calls, answers = node_calls
    answers["import hivex"] = (1, '', '')
    answers['apt-get install'] = (1, '', '')

    v2p._inject_virtio_drivers(_Manager(), _Task())
    assert 'libhivex-bin' in _apt_command(calls)


def test_a_missing_hivex_shell_does_not_abort_a_migration_that_used_to_work(node_calls):
    # hivexsh only feeds the version probe, which has its own fallback (the w11/amd64
    # default), so its absence must not gate the tools the injection cannot run at all
    # without: doing that would turn an air-gapped node that worked into a failed
    # migration. It is checked and installed separately, after the required probe,
    # and its own failure is logged rather than fatal.
    calls, answers = node_calls
    answers['command -v hivexsh'] = (1, '', '')
    answers['libhivex-bin'] = (1, '', '')

    v2p._inject_virtio_drivers(_Manager(), _Task())
    assert 'hivexsh' not in calls[0]
    assert any('command -v hivexsh' in c for c in calls)
    # Execution kept going past the failed hivexsh install rather than stopping there.
    assert any('test -f' in c for c in calls)


def _injection_script(calls):
    """The one long script the injection hands to the node, once everything resolved."""
    return next(cmd for cmd in calls if 'NO_NTFS_FOUND' in cmd)


@pytest.fixture
def resolved_node(node_calls):
    """A node where every lookup succeeds, so the script itself gets built."""
    calls, answers = node_calls
    answers['pvesm path'] = (0, '/dev/zvol/tank/vm-100-disk-0\n', '')
    answers['pvesm status'] = (0, 'zfspool\n', '')
    return calls, answers


def test_catalogues_go_to_the_catalogue_store(resolved_node):
    # A .cat next to the .sys file is never read. Windows looks for a driver catalogue
    # in the store below, which is where its own catalogues live too.
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert 'System32/CatRoot/{F750E6C3-38EE-11D1-85E5-00C04FC295EE}' in script
    assert 'cp -f "$SRC"/*.cat "$CAT_DEST/"' in script


def test_catalogues_do_not_land_among_the_driver_binaries(resolved_node):
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    assert 'cp -f "$SRC"/*.cat "$DRV_DEST/"' not in _injection_script(calls)


# ---------------------------------------------------------------------------
# What the driver expects, per driver and per Windows version
# ---------------------------------------------------------------------------

def test_the_scsi_driver_gets_the_bus_type_its_inf_asks_for(resolved_node):
    """vioscsi.inf writes 0x0000000A, and the injection has to write what the INF writes.

    Read off every variant the virtio-win ISO ships, 2k8 through 2k25, where the value is
    0x0000000A without exception. This pins the value against the driver's own INF; it does
    not claim that the value alone decides whether a given guest boots.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "('vioscsi', 0x59, 'system32\\\\drivers\\\\vioscsi.sys', 0x0A)" in script


def test_the_block_driver_keeps_the_bus_type_its_own_inf_asks_for(resolved_node):
    """viostor.inf writes 0x00000001, and that one was never wrong.

    The defect was writing a single value for both drivers. Correcting it by moving both
    to 0x0A would break the direction that used to work.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "('viostor', 0x58, 'system32\\\\drivers\\\\viostor.sys', 0x01)" in script


def test_the_second_value_both_infs_set_is_written_too(resolved_node):
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    assert "set_dword(params, 'DmaRemappingCompatible', 0)" in _injection_script(calls)


def test_the_modern_scsi_device_id_is_recognised(resolved_node):
    """1048 is VirtIO SCSI; 1041, which stood here, is VirtIO network.

    A guest that sees the controller as a modern rather than a transitional device never
    matched the CriticalDeviceDatabase entry.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "('pci#ven_1af4&dev_1048', 'vioscsi')" in script
    assert "('pci#ven_1af4&dev_1041', 'vioscsi')" not in script


@pytest.mark.parametrize('product,build,expected', [
    ('Windows Server 2012 Standard', '9200', '2k12/amd64'),
    ('Windows Server 2012 R2 Standard', '9600', '2k12R2/amd64'),
    ('Windows Server 2016 Standard', '14393', '2k16/amd64'),
    ('Windows Server 2019 Standard', '17763', '2k19/amd64'),
    ('Windows Server 2022 Standard', '20348', '2k22/amd64'),
    ('Windows Server 2025 Standard', '26100', '2k25/amd64'),
    # The build number alone has to be enough: on Server SKUs ProductName is unreliable,
    # and a host without the hivex shell reports nothing at all for it.
    ('', '9200', '2k12/amd64'),
    ('', '14393', '2k16/amd64'),
    ('', '20348', '2k22/amd64'),
    ('', '26100', '2k25/amd64'),
    # A later build of the same release still belongs to that release.
    ('', '26200', '2k25/amd64'),
])
def test_every_windows_version_in_the_matrix_picks_its_own_drivers(product, build, expected):
    """docs/hyperv-windows-matrix.md is the prose; this is the part that fails on a change."""
    assert v2p._detect_windows_driver_subdir(product, build) == expected


# ---------------------------------------------------------------------------
# Which control set the registry work lands in
# ---------------------------------------------------------------------------

def test_the_drivers_are_registered_in_every_control_set(resolved_node):
    """Not only ControlSet001, because that is not the only set that can be booted.

    Select\\Current names the active set and Select\\LastKnownGood the set Windows falls
    back to after a failed boot. Measured on freshly installed Server 2012 R2, 2016 and
    2025 images: Current was 1 and LastKnownGood was 2, and ControlSet002 held no storage
    driver at all. A single failed boot would therefore have moved the guest into a
    control set that cannot reach its own disk.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "for cs_name in control_sets:" in script
    assert "navigate(root, ['ControlSet001'])" not in script


def test_the_control_sets_are_discovered_rather_than_listed(resolved_node):
    """A hive may carry ControlSet003; a fixed list would silently skip it."""
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "h.node_name(c).lower().startswith('controlset')" in script
    assert "['ControlSet001','ControlSet002']" not in script


def test_the_first_boot_service_reaches_the_same_control_sets(resolved_node):
    """The service that installs the driver package properly had the same gap."""
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert script.count("h.node_name(c).lower().startswith('controlset')") == 2


# ---------------------------------------------------------------------------
# The guest agent, which the driver MSI does not contain
# ---------------------------------------------------------------------------

def _staging_part(script):
    """The shell lines that copy the installers from the ISO into C:\\qemu."""
    start = script.index('PEGADIR=')
    return script[start:script.index('SYSTEM_HIVE=', start)]


def _first_boot_command(script):
    """The command line the first-boot service runs, as Windows will receive it."""
    service = _embedded_python(script)[1]
    line = next(l for l in service.splitlines() if l.startswith('cmdline = '))
    scope = {}
    exec(line, scope)
    return scope['cmdline']


def test_the_guest_agent_installer_is_staged_from_the_iso(resolved_node, tmp_path):
    """virtio-win ships the agent as guest-agent/qemu-ga-x86_64.msi. Checked against
    0.1.302: virtio-win-gt-x64.msi names vioscsi and blnsvr and never qemu-ga."""
    import subprocess
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    iso = tmp_path / 'iso'
    (iso / 'guest-agent').mkdir(parents=True)
    (iso / 'virtio-win-gt-x64.msi').write_bytes(b'drivers')
    (iso / 'guest-agent' / 'qemu-ga-x86_64.msi').write_bytes(b'agent')
    (tmp_path / 'win' / 'Windows').mkdir(parents=True)
    done = subprocess.run(
        ['bash', '-c', _staging_part(_injection_script(calls))], capture_output=True,
        text=True, env={'PATH': '/usr/bin:/bin', 'ISO_MNT': str(iso),
                        'WIN_MNT': str(tmp_path / 'win'), 'WDIR': 'Windows'})

    assert 'AGENT_STAGED qemu-ga-x86_64.msi' in done.stdout, done.stdout + done.stderr
    assert (tmp_path / 'win' / 'qemu' / 'qemu-ga-x86_64.msi').read_bytes() == b'agent'


def test_an_iso_without_the_agent_says_so(resolved_node, tmp_path):
    import subprocess
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    iso = tmp_path / 'iso'
    iso.mkdir()
    (iso / 'virtio-win-gt-x64.msi').write_bytes(b'drivers')
    (tmp_path / 'win' / 'Windows').mkdir(parents=True)
    done = subprocess.run(
        ['bash', '-c', _staging_part(_injection_script(calls))], capture_output=True,
        text=True, env={'PATH': '/usr/bin:/bin', 'ISO_MNT': str(iso),
                        'WIN_MNT': str(tmp_path / 'win'), 'WDIR': 'Windows'})

    assert 'AGENT_MISSING' in done.stdout
    assert 'MSI_STAGED virtio-win-gt-x64.msi' in done.stdout


def test_the_first_boot_service_only_launches_the_staged_script(resolved_node):
    """The installs run in firstboot.ps1 (tests/test_virtio_firstboot.py), not in the
    service's own start, where the SCM holds its database lock."""
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    command = _first_boot_command(_injection_script(calls))
    assert command == FIRST_BOOT_SERVICE_COMMAND
    assert 'msiexec' not in command


def test_the_first_boot_script_is_staged_with_the_installers(resolved_node, tmp_path):
    import subprocess
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    iso = tmp_path / 'iso'
    iso.mkdir()
    (iso / 'virtio-win-gt-x64.msi').write_bytes(b'drivers')
    (tmp_path / 'win' / 'Windows').mkdir(parents=True)
    done = subprocess.run(
        ['bash', '-c', _staging_part(_injection_script(calls))], capture_output=True,
        text=True, env={'PATH': '/usr/bin:/bin', 'ISO_MNT': str(iso),
                        'WIN_MNT': str(tmp_path / 'win'), 'WDIR': 'Windows'})

    assert 'FIRSTBOOT_STAGED firstboot.ps1' in done.stdout, done.stdout + done.stderr
    assert (tmp_path / 'win' / 'qemu' / 'firstboot.ps1').read_bytes() == \
        FIRST_BOOT_SCRIPT.replace('\n', '\r\n').encode('utf-8')


def test_without_an_installer_no_first_boot_script_is_staged(resolved_node, tmp_path):
    import subprocess
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    (tmp_path / 'iso').mkdir()
    (tmp_path / 'win' / 'Windows').mkdir(parents=True)
    done = subprocess.run(
        ['bash', '-c', _staging_part(_injection_script(calls))], capture_output=True,
        text=True, env={'PATH': '/usr/bin:/bin', 'ISO_MNT': str(tmp_path / 'iso'),
                        'WIN_MNT': str(tmp_path / 'win'), 'WDIR': 'Windows'})

    assert 'MSI_MISSING' in done.stdout
    assert not (tmp_path / 'win' / 'qemu' / 'firstboot.ps1').exists()


def test_the_service_is_registered_only_when_the_script_was_staged(resolved_node):
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    service = _embedded_python(_injection_script(calls))[1]
    guard = service.index("os.path.join(sys.argv[3], 'firstboot.ps1')")
    assert guard < service.index("set_exp(svc, 'ImagePath', cmdline)")


def test_the_staging_folder_is_named_qemu(resolved_node, tmp_path):
    import subprocess
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    iso = tmp_path / 'iso'
    iso.mkdir()
    (iso / 'virtio-win-gt-x64.msi').write_bytes(b'drivers')
    (tmp_path / 'win' / 'Windows').mkdir(parents=True)
    subprocess.run(
        ['bash', '-c', _staging_part(_injection_script(calls))], capture_output=True,
        text=True, env={'PATH': '/usr/bin:/bin', 'ISO_MNT': str(iso),
                        'WIN_MNT': str(tmp_path / 'win'), 'WDIR': 'Windows'})

    assert (tmp_path / 'win' / 'qemu' / 'virtio-win-gt-x64.msi').read_bytes() == b'drivers'
    assert not (tmp_path / 'win' / 'PegaProx').exists()
    assert 'C:\\PegaProx' not in _first_boot_command(_injection_script(calls))


def test_what_was_staged_reaches_the_migration_log(node_calls):
    calls, answers = node_calls
    answers['pvesm path'] = (0, '/dev/zvol/tank/vm-100-disk-0\n', '')
    answers['pvesm status'] = (0, 'zfspool\n', '')
    answers['bash /tmp/v2p-virtio-inject-'] = (
        0, 'MSI_STAGED virtio-win-gt-x64.msi\nAGENT_STAGED qemu-ga-x86_64.msi\n'
           'HIVEX have_viostor=True have_vioscsi=True\nINJECTION_OK\n', '')
    task = _Task()
    v2p._inject_virtio_drivers(_Manager(), task)
    assert '[VirtIO] AGENT_STAGED qemu-ga-x86_64.msi' in task.lines


# ---------------------------------------------------------------------------
# The scripts are built from strings, so nothing else checks their syntax
# ---------------------------------------------------------------------------

def _embedded_python(script):
    """Every `python3 - << 'PYxxx'` block inside the shell script the node receives."""
    blocks, current, marker = [], None, None
    for line in script.splitlines():
        if current is None:
            start = re.search(r"<< '(PY\w+)'", line)
            if start:
                current, marker = [], start.group(1)
            continue
        if line.strip() == marker:
            blocks.append('\n'.join(current))
            current = None
            continue
        current.append(line)
    return blocks


def test_the_embedded_registry_scripts_are_valid_python(resolved_node):
    """A syntax error in a string-built script only shows up during a migration.

    The injection composes two Python programs by concatenating string literals. Nothing
    parses them until a node runs them, and a node that fails to run them reports
    'HIVEX_MERGE_FAILED' with no line number.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    blocks = _embedded_python(_injection_script(calls))
    assert len(blocks) == 2, f'expected two embedded programs, found {len(blocks)}'
    for index, block in enumerate(blocks):
        compile(block, f'<embedded {index}>', 'exec')


# ---------------------------------------------------------------------------
# Where a boot-critical driver is bound to its device
# ---------------------------------------------------------------------------

def test_the_driver_is_registered_in_the_driver_database(resolved_node):
    """Windows 8 and Server 2012 and newer bind a boot device through the DriverDatabase.

    They do not read the CriticalDeviceDatabase any more. A guest registered only the old
    way loads the driver and then cannot attach it to its controller, which the kernel
    reports as INACCESSIBLE_BOOT_DEVICE. Measured on freshly installed Server 2016, 2022
    and 2025, each with the service entry, the driver file and the CriticalDeviceDatabase
    correct: all three opened the recovery environment, and all three reach the login
    screen once this is written.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "ddb = h.node_get_child(root, 'DriverDatabase')" in script
    assert "navigate(ddb, ['DriverInfFiles', inf])" in script
    assert "navigate(ddb, ['DriverPackages', label])" in script
    assert "navigate(ddb, ['DeviceIds', 'PCI', pci_id])" in script


def test_the_old_database_is_written_as_well(resolved_node):
    """Windows 7 and Server 2008 R2 have no DriverDatabase and still need the old entries."""
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "cdb = navigate(navigate(cs, ['Control']), ['CriticalDeviceDatabase'])" in script


def test_the_driver_package_does_not_take_a_name_windows_may_already_use(resolved_node):
    """A guest may already have a package under the driver's own INF name.

    A Server 2025 image was found carrying a virtio catalogue of its own, and an entry
    written under that name would replace whatever it belongs to.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "inf = 'pegaprox_' + svc + '.inf'" in script


@pytest.mark.parametrize('driver, device_id', [
    ('vioscsi', 'VEN_1AF4&DEV_1004&REV_00'),        # transitional
    ('vioscsi', 'VEN_1AF4&DEV_1048&REV_01'),        # modern
    ('viostor', 'VEN_1AF4&DEV_1001&REV_00'),
    ('viostor', 'VEN_1AF4&DEV_1042&REV_01'),
])
def test_both_the_transitional_and_the_modern_device_are_bound(resolved_node, driver,
                                                               device_id):
    """Which of the two a guest is given depends on the machine type it was built with."""
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    line = next(l for l in script.splitlines() if f"_devices['{driver}']" in l)
    assert device_id in line


# ---------------------------------------------------------------------------
# A driver the loader would refuse
# ---------------------------------------------------------------------------

def test_an_unsignable_driver_is_not_made_boot_critical(resolved_node):
    """Registering it produces a guest that stops at 0xc0000428 before the kernel starts.

    virtio-win stopped having the drivers for out-of-support Windows versions signed
    through Microsoft after release 0.1.208; from 0.1.221 the 2012 R2 variants carry
    'virtio-win / Red Hat Inc.' and nothing else. The release used for Server 2012 R2 is
    0.1.189, whose 2k12R2 drivers chain to Microsoft Code Verification Root.
    """
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    assert "def boot_signable(path):" in script
    assert "_signable['vioscsi']" in script
    assert "b'Microsoft Code Verification Root'" in script


def test_the_signature_is_read_from_the_file_not_the_windows_version(resolved_node):
    """An operator supplying an older ISO for an old guest has a driver that does load."""
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    script = _injection_script(calls)
    #: data directory 4 is the certificate table
    assert "struct.unpack_from('<II', data, directories + 32)" in script


def test_a_refused_signature_is_reported_rather_than_called_success(node_calls,
                                                                    resolved_node):
    calls, answers = resolved_node
    answers['bash /tmp/v2p-virtio-inject'] = (
        0, 'COPIED vioscsi\nBOOT_SIGNATURE_MISSING vioscsi\nINJECTION_OK\n', '')
    task = _Task()

    assert v2p._inject_virtio_drivers(_Manager(), task) is False
    assert any('not registered as one' in line for line in task.lines)


def test_no_storage_driver_registered_is_not_a_success(resolved_node):
    """The staged-driver count includes the network card, so it cannot decide this.

    A driver ISO whose chosen subdirectory holds netkvm but neither viostor nor vioscsi
    would otherwise produce a tick and a VM with no way to reach its own disk.
    """
    calls, answers = resolved_node
    answers['bash /tmp/v2p-virtio-inject'] = (
        0, 'COPIED NetKVM\nHIVEX have_viostor=False have_vioscsi=False\nINJECTION_OK\n', '')
    task = _Task()

    assert v2p._inject_virtio_drivers(_Manager(), task) is False
    assert any('no way to reach its disk' in line for line in task.lines)


class TestTheHibernationOnlyModeReachesItsOwnWork:
    """The script that mode generates, read rather than assumed.

    Every other test here monkeypatches `_inject_virtio_drivers` away, so nothing looked
    at what it builds — and it mounted the driver ISO unconditionally. `$ISO` is empty in
    this mode, so the run exited 3 with ISO_MOUNT_FAILED before reaching the two lines the
    mode exists for. Shipped that way it would have been a no-op with a success-shaped log.
    """

    @staticmethod
    def _script(clear_hibernation_only):
        from unittest.mock import MagicMock
        from pegaprox.core import v2p

        captured = {}

        def node_exec(_mgr, _node, command, timeout=600, **_kw):
            if 'CLEAN_ONLY=' in command:
                captured['script'] = command
            if 'pvesm path' in command:
                return 0, '/dev/zvol/vmstorage/vm-120-disk-0', ''
            if 'pvesm status' in command:
                return 0, 'zfspool', ''
            return 0, '', ''

        task = MagicMock()
        task.proxmox_vmid, task.target_node = 120, 'node-a'
        task.target_storage, task.virtio_iso_path = 'vmstorage', ''
        task.install_virtio_drivers = not clear_hibernation_only
        mgr = MagicMock()
        mgr.host, mgr.api_port = '127.0.0.1', 8006
        mgr._api_get.return_value = MagicMock(
            status_code=200, json=lambda: {'data': {'status': 'stopped'}})
        v2p._inject_virtio_drivers(mgr, task, node_exec=node_exec,
                                   clear_hibernation_only=clear_hibernation_only)
        return captured.get('script', '')

    def test_the_iso_mount_is_skipped_without_an_iso(self):
        script = self._script(True)
        assert 'CLEAN_ONLY=1' in script
        assert 'if [ "$CLEAN_ONLY" != 1 ]' in script, \
            'the ISO mount is unconditional and this mode dies on it'

    def test_it_still_reaches_the_two_lines_it_exists_for(self):
        script = self._script(True)
        assert script.index('ntfsfix') < script.index('HIBERNATION_CLEARED')
        assert script.index('remove_hiberfile') < script.index('HIBERNATION_CLEARED')

    def test_the_driver_path_still_mounts_its_iso(self):
        script = self._script(False)
        assert 'CLEAN_ONLY=0' in script
        assert 'mount -o ro,loop' in script


# ===========================================================================
# Which partition is the Windows volume
# ===========================================================================

def _partition_choice(script):
    """The shell lines that decide which partition is mounted as the Windows volume."""
    start = script.index('NTFS_PARTS=$(')
    end = script.index('echo "WIN_PART=$WIN_PART"', start)
    return script[start:script.index('\n', end) + 1]


def _choose_partition(script, tmp_path, partitions):
    """Run the choice against fake partitions: {name: (size, fstype, has_windows)}.

    `blkid`, `blockdev`, `mount` and `umount` are stand-ins on PATH, and `[ -b ]` answers
    yes for the fake partition files, so the real shell logic runs without a block device.
    A "mount" copies the partition's fixture directory into the mount point.
    """
    import subprocess

    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    loop = tmp_path / 'loop0'
    for name, (size, fstype, has_windows) in partitions.items():
        part = tmp_path / f'loop0{name}'
        part.write_text(f'{size} {fstype}\n')
        content = tmp_path / 'content' / f'loop0{name}'
        content.mkdir(parents=True)
        if has_windows:
            (content / 'Windows' / 'System32' / 'config').mkdir(parents=True)
    tools = {
        'blkid': 'cut -d" " -f2 "${@: -1}"',
        'blockdev': 'cut -d" " -f1 "${@: -1}"',
        'mount': f'cp -R {tmp_path}/content/$(basename "${{@: -2:1}}")/. "${{@: -1}}"/ '
                 f'&& echo "$*" >> {tmp_path}/mounts',
        'umount': 'find "$1" -mindepth 1 -delete',
    }
    for name, body in tools.items():
        tool = fake_bin / name
        tool.write_text(f'#!/bin/bash\n{body}\n')
        tool.chmod(0o755)
    win_mnt = tmp_path / 'win'
    win_mnt.mkdir()
    prelude = ('[() { if builtin test "$1" = -b; then builtin test -f "$2"; return; fi; '
               'builtin test "${@:1:$#-1}"; }\n')
    done = subprocess.run(
        ['bash', '-c', prelude + _partition_choice(script)], capture_output=True, text=True,
        env={'PATH': f'{fake_bin}:/usr/bin:/bin', 'LOOP': str(loop), 'WIN_MNT': str(win_mnt)})
    mounts = (tmp_path / 'mounts').read_text() if (tmp_path / 'mounts').exists() else ''
    return done.stdout + done.stderr, mounts


def test_the_windows_partition_is_chosen_over_a_larger_data_partition(resolved_node, tmp_path):
    """800 GB disk: Windows on 100 GB, data on 700 GB. The largest one is the wrong one."""
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    out, mounts = _choose_partition(_injection_script(calls), tmp_path, {
        'p1': (500 * 2**20, 'vfat', False),
        'p2': (100 * 2**30, 'ntfs', True),
        'p3': (700 * 2**30, 'ntfs', False),
    })

    assert f'WIN_PART={tmp_path}/loop0p2' in out, out
    assert 'WINDOWS_PARTITION_NOT_IDENTIFIED' not in out


def test_partitions_are_only_looked_at_read_only_while_choosing(resolved_node, tmp_path):
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    _, mounts = _choose_partition(_injection_script(calls), tmp_path, {
        'p1': (100 * 2**30, 'ntfs', True),
        'p2': (700 * 2**30, 'ntfs', False),
    })

    assert mounts, 'no candidate was looked at'
    assert all('-o ro ' in line for line in mounts.splitlines()), mounts


def test_without_a_recognisable_windows_the_largest_is_kept_and_said(resolved_node, tmp_path):
    calls, _ = resolved_node
    v2p._inject_virtio_drivers(_Manager(), _Task())

    out, _ = _choose_partition(_injection_script(calls), tmp_path, {
        'p1': (100 * 2**30, 'ntfs', False),
        'p2': (700 * 2**30, 'ntfs', False),
    })

    assert 'WINDOWS_PARTITION_NOT_IDENTIFIED' in out, out
    assert f'WIN_PART={tmp_path}/loop0p2' in out, out
