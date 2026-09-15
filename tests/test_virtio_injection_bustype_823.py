"""The registry values the offline VirtIO injection writes for the storage drivers.

Reported against 1.1.1: `Parameters\\BusType` was written as 1 for both viostor and
vioscsi, and the CriticalDeviceDatabase named DEV_1041 -- VirtIO network -- as the
modern VirtIO SCSI device. A Windows guest whose boot disk was moved to
`virtio-scsi-single` afterwards stopped with INACCESSIBLE_BOOT_DEVICE before usermode.
The values come out of the vendor INFs: viostor.inf writes 0x00000001,
vioscsi.inf writes 0x0000000A, and both write DmaRemappingCompatible.

These tests do not grep the source. They lift the python program the injection hands
to the node, run it against a fake hivex, and read back what it wrote -- so they fail
when the behaviour is lost, not when a comment is reworded.
"""
import os
import sys

import pytest

import pegaprox.core.v2p as v2p


# ── the node the injection thinks it is talking to ───────────────────────────

class _Task:
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
    host = '127.0.0.1'
    api_port = 8006

    def _api_get(self, url):
        raise RuntimeError('no API in this test')


@pytest.fixture
def node_script(monkeypatch):
    """The one long script `_inject_virtio_drivers` hands to a node where all lookups win."""
    calls = []

    def fake_exec(pve_mgr, node, cmd, timeout=600, **kwargs):
        calls.append(cmd)
        if 'pvesm path' in cmd:
            return 0, '/dev/zvol/tank/vm-100-disk-0\n', ''
        if 'pvesm status' in cmd:
            return 0, 'zfspool\n', ''
        return 0, '', ''

    monkeypatch.setattr(v2p, '_pve_node_exec', fake_exec)
    v2p._inject_virtio_drivers(_Manager(), _Task())
    script = next(cmd for cmd in calls if 'NO_NTFS_FOUND' in cmd)
    return script


def _registry_program(script):
    """The python program run against the SYSTEM hive, out of its heredoc."""
    assert script.count("<< 'PYEOF'") == 1, "more than one PYEOF heredoc to choose from"
    # The redirection carries a `|| { ... }` tail, so the body starts after that line.
    body = script.split("<< 'PYEOF'", 1)[1].split('\n', 1)[1]
    body = body.split('\nPYEOF\n', 1)[0]
    assert 'hivex' in body, "that heredoc is not the registry program"
    return body


# ── a hive that records instead of writing ───────────────────────────────────

class _FakeNode:
    def __init__(self, name, parent=None):
        self.name = name
        self.children = {}
        self.values = {}
        self.path = f'{parent.path}\\{name}' if parent is not None and parent.path else name


class _FakeHive:
    """Enough of hivex.Hivex for the injection's program, plus a readable result."""

    def __init__(self, path, write=False):
        self.path = path
        self.write = write
        self.committed = False
        self._root = _FakeNode('', None)
        self._root.path = ''

    def root(self):
        return self._root

    def node_get_child(self, node, name):
        return node.children.get(name)

    def node_add_child(self, node, name):
        child = _FakeNode(name, node)
        node.children[name] = child
        return child

    def node_set_value(self, node, value):
        node.values[value['key']] = (value['t'], value['value'])

    def commit(self, _arg):
        self.committed = True

    # -- readback -------------------------------------------------------------

    def node(self, path):
        n = self._root
        for part in path.split('\\'):
            n = n.children.get(part)
            if n is None:
                return None
        return n

    def dword(self, path, key):
        n = self.node(path)
        assert n is not None, f'no such key: {path}'
        assert key in n.values, f'{path} has no value {key}'
        kind, raw = n.values[key]
        assert kind == _REG_DWORD, f'{path}\\{key} is not a REG_DWORD'
        return int.from_bytes(raw, 'little')

    def string(self, path, key):
        n = self.node(path)
        assert n is not None, f'no such key: {path}'
        kind, raw = n.values[key]
        assert kind in (_REG_SZ, _REG_EXPAND_SZ)
        return raw.decode('utf-16-le').rstrip('\x00')


_REG_DWORD, _REG_SZ, _REG_EXPAND_SZ = 4, 1, 2


class _FakeHivexModule:
    Hivex = _FakeHive


class _FakeHiveTypes:
    REG_DWORD = _REG_DWORD
    REG_SZ = _REG_SZ
    REG_EXPAND_SZ = _REG_EXPAND_SZ


def _windows_tree(tmp_path, drivers=('viostor.sys', 'vioscsi.sys')):
    """The part of the mounted guest filesystem the program looks at."""
    config = tmp_path / 'Windows' / 'System32' / 'config'
    config.mkdir(parents=True)
    hive = config / 'SYSTEM'
    hive.write_bytes(b'')
    drv = tmp_path / 'Windows' / 'System32' / 'drivers'
    drv.mkdir(parents=True)
    for name in drivers:
        (drv / name).write_bytes(b'')
    return hive


def _run_registry_program(script, tmp_path, monkeypatch, argv_extra=(),
                          drivers=('viostor.sys', 'vioscsi.sys')):
    hive_path = _windows_tree(tmp_path, drivers)
    hives = []

    class _Recording(_FakeHive):
        def __init__(self, path, write=False):
            super().__init__(path, write)
            hives.append(self)

    monkeypatch.setitem(sys.modules, 'hivex',
                        type('m', (), {'Hivex': _Recording, 'hive_types': _FakeHiveTypes})())
    monkeypatch.setitem(sys.modules, 'hivex.hive_types', _FakeHiveTypes)
    monkeypatch.setattr(sys, 'argv', ['-', str(hive_path), *argv_extra])

    exec(compile(_registry_program(script), '<injection>', 'exec'), {'__name__': '__main__'})
    assert hives, 'the program never opened the hive'
    assert hives[0].committed, 'the program never committed'
    return hives[0]


@pytest.fixture
def written(node_script, tmp_path, monkeypatch):
    return _run_registry_program(node_script, tmp_path, monkeypatch)


# ── BusType belongs to the driver, not to the pair ───────────────────────────

_PARAMS = 'ControlSet001\\Services\\{}\\Parameters'


def test_the_scsi_driver_gets_the_bus_type_its_own_inf_asks_for(written):
    """vioscsi.inf writes 0x0000000A -- every variant the ISO ships, 2k8 through 2k25."""
    assert written.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A


def test_the_block_driver_keeps_the_value_that_was_never_wrong(written):
    """viostor.inf writes 0x00000001. Moving both drivers to 0x0A would break the
    direction that used to work, so the value has to be carried per driver."""
    assert written.dword(_PARAMS.format('viostor'), 'BusType') == 0x01


@pytest.mark.parametrize('driver', ['viostor', 'vioscsi'])
def test_the_second_value_both_infs_set_is_written_too(written, driver):
    assert written.dword(_PARAMS.format(driver), 'DmaRemappingCompatible') == 0


@pytest.mark.parametrize('driver', ['viostor', 'vioscsi'])
def test_the_rest_of_the_service_entry_is_unchanged(written, driver):
    """The tuple grew a field; nothing that was already right may fall out of it."""
    svc = f'ControlSet001\\Services\\{driver}'
    assert written.string(svc, 'ImagePath') == f'system32\\drivers\\{driver}.sys'
    assert written.dword(svc, 'Start') == 0
    assert written.dword(svc, 'Type') == 1
    assert written.string(svc, 'Group') == 'SCSI miniport'
    assert written.dword(f'{svc}\\Parameters\\PnpInterface', '5') == 1


def test_a_driver_whose_file_never_arrived_is_still_not_registered(node_script, tmp_path,
                                                                   monkeypatch):
    """Start=0 for a miniport that is not on disk is the bluescreen this patch is about,
    from the other direction."""
    hive = _run_registry_program(node_script, tmp_path, monkeypatch,
                                 drivers=('viostor.sys',))
    assert hive.node(_PARAMS.format('viostor')) is not None
    assert hive.node('ControlSet001\\Services\\vioscsi') is None


# ── the device id the CriticalDeviceDatabase is keyed by ─────────────────────

_CDB = 'ControlSet001\\Control\\CriticalDeviceDatabase'


def test_the_modern_scsi_device_is_bound_to_the_scsi_driver(written):
    """0x1040 + device type: type 8 is SCSI, so the modern id is 1048."""
    assert written.string(f'{_CDB}\\pci#ven_1af4&dev_1048', 'Service') == 'vioscsi'


def test_the_modern_scsi_device_is_bound_in_its_subsystem_form_as_well(written):
    key = f'{_CDB}\\pci#ven_1af4&dev_1048&subsys_11001af4&rev_01'
    assert written.string(key, 'Service') == 'vioscsi'


def test_the_transitional_device_is_still_bound(written):
    """A `pc` machine with virtio-scsi-single reports 1004; that entry was correct."""
    assert written.string(f'{_CDB}\\pci#ven_1af4&dev_1004', 'Service') == 'vioscsi'


def test_the_network_device_is_not_claimed_by_the_scsi_driver(written):
    """1041 is type 1, VirtIO network -- it is what netkvm.inf matches."""
    assert written.node(f'{_CDB}\\pci#ven_1af4&dev_1041') is None


@pytest.mark.parametrize('key', [
    'pci#ven_1af4&dev_1001',
    'pci#ven_1af4&dev_1001&subsys_00021af4&rev_00',
])
def test_the_block_drivers_entries_are_left_alone(written, key):
    assert written.string(f'{_CDB}\\{key}', 'Service') == 'viostor'


# ── the program itself ───────────────────────────────────────────────────────

def test_the_registry_program_is_valid_python(node_script):
    """It is assembled from string literals and runs on a customer's hypervisor."""
    compile(_registry_program(node_script), '<injection>', 'exec')


# ── the values come out of the INF that ships beside the driver ──────────────
#
# Marcus Kellermann on #823: hard-coding what the vendor writes is correct until the
# vendor changes it. The INF is in the directory the .sys was copied from, so read it
# there and keep the per-driver pair as the fallback.

# What virtio-win actually ships, trimmed to the lines that matter.
_VIOSCSI_INF = """\
; vioscsi.inf
[Version]
Signature="$WINDOWS NT$"
Class=SCSIAdapter

[vioscsi_RegistryAddReg]
HKR,"Parameters\\PnpInterface","5",0x00010001,0x00000001
HKR, "Parameters", "BusType", 0x00010001, 0x0000000A
HKR, "Parameters", "DmaRemappingCompatible", 0x00010001, 0
"""

_VIOSTOR_INF = """\
; viostor.inf
[viostor_RegistryAddReg]
HKR, "Parameters", "BusType", 0x00010001, 0x00000001
HKR, "Parameters", "DmaRemappingCompatible", 0x00010001, 0
"""


@pytest.fixture
def inf_dir(tmp_path):
    d = tmp_path / 'iso' / 'amd64'
    d.mkdir(parents=True)
    return d


def _with_infs(node_script, tmp_path, monkeypatch, inf_dir):
    return _run_registry_program(node_script, tmp_path, monkeypatch,
                                 argv_extra=(str(inf_dir), str(inf_dir)))


def test_the_values_are_read_from_the_shipped_inf(node_script, tmp_path, monkeypatch,
                                                  inf_dir):
    (inf_dir / 'vioscsi.inf').write_text(_VIOSCSI_INF)
    (inf_dir / 'viostor.inf').write_text(_VIOSTOR_INF)

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A
    assert hive.dword(_PARAMS.format('viostor'), 'BusType') == 0x01


def test_a_changed_inf_changes_what_is_written(node_script, tmp_path, monkeypatch,
                                               inf_dir):
    """The point of reading it: a virtio-win release that moves the value moves with it,
    without this file being edited. A value no ISO ships, so only the INF can produce it.
    """
    (inf_dir / 'vioscsi.inf').write_text(
        _VIOSCSI_INF.replace('0x0000000A', '0x0000000C'))

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0C


def test_the_second_value_is_read_the_same_way(node_script, tmp_path, monkeypatch,
                                               inf_dir):
    (inf_dir / 'vioscsi.inf').write_text(
        _VIOSCSI_INF.replace('"DmaRemappingCompatible", 0x00010001, 0',
                             '"DmaRemappingCompatible", 0x00010001, 1'))

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'DmaRemappingCompatible') == 1


def test_a_utf16_inf_is_read_too(node_script, tmp_path, monkeypatch, inf_dir):
    """INF files are shipped in both encodings; a mojibake read would silently fall back."""
    (inf_dir / 'vioscsi.inf').write_bytes(
        _VIOSCSI_INF.replace('0x0000000A', '0x0000000C').encode('utf-16'))

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0C


def test_a_missing_inf_leaves_the_built_in_value(node_script, tmp_path, monkeypatch,
                                                 inf_dir):
    """An ISO layout with no INF next to the .sys must not turn into no value at all."""
    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A
    assert hive.dword(_PARAMS.format('viostor'), 'BusType') == 0x01
    assert hive.dword(_PARAMS.format('vioscsi'), 'DmaRemappingCompatible') == 0


def test_a_node_that_reports_no_directory_at_all_still_writes_the_values(written):
    """The injection passes two more arguments now; an older shell that passes none, or a
    driver that was skipped, has to keep working."""
    assert written.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A


@pytest.mark.parametrize('line', [
    'HKR, "Parameters", "BusType", 0x00000000, 0x0000000C',   # not a DWORD write
    'HKR, "Parameters\\Other", "BusType", 0x00010001, 0x0C',  # a different subkey
    'HKLM, "Parameters", "BusType", 0x00010001, 0x0C',        # not HKR
    '; HKR, "Parameters", "BusType", 0x00010001, 0x0C',       # commented out
    'HKR, "Parameters", "BusType", 0x00010001, notanumber',
])
def test_a_line_that_does_not_write_this_dword_is_ignored(node_script, tmp_path,
                                                          monkeypatch, inf_dir, line):
    (inf_dir / 'vioscsi.inf').write_text(_VIOSCSI_INF + line + '\n')

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A


def test_an_unreadable_inf_does_not_abort_the_injection(node_script, tmp_path,
                                                        monkeypatch, inf_dir):
    """A directory where a file is expected raises on open; the hive still gets written."""
    (inf_dir / 'vioscsi.inf').mkdir()

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A


def test_the_injection_hands_the_node_both_directories(node_script):
    """Read from the ISO the .sys came from, not from the guest's INF directory: the
    copy into the guest is best-effort and its spelling differs between Windows versions.
    """
    assert 'VIOSTOR_SRC="$SRC"' in node_script
    assert 'VIOSCSI_SRC="$SRC"' in node_script
    assert '"$SYSTEM_HIVE" "${VIOSTOR_SRC:-}" "${VIOSCSI_SRC:-}"' in node_script


def test_no_directory_means_no_file_is_opened_at_all(node_script, tmp_path, monkeypatch):
    """A driver that was skipped hands over an empty string. Joining that with the file
    name gives a *relative* path, which would read whatever happens to sit in the node
    script's working directory."""
    stray = tmp_path / 'cwd'
    stray.mkdir()
    (stray / 'vioscsi.inf').write_text(
        _VIOSCSI_INF.replace('0x0000000A', '0x0000000C'))
    monkeypatch.chdir(stray)

    hive = _run_registry_program(node_script, tmp_path, monkeypatch, argv_extra=('', ''))
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A


# ── the INF's own [Strings] section ──────────────────────────────────────────
#
# Both of these came out of a Codex review of the two commits above.

_INF_WITH_STRINGS = """\
[vioscsi_RegistryAddReg]
HKR, "Parameters", "BusType", %REG_DWORD%, %BusTypeValue%
HKR, "Parameters", "DmaRemappingCompatible", %REG_DWORD%, 0

[Strings]
REG_DWORD      = 0x00010001
BusTypeValue   = "0x0000000C"
"""


def test_a_symbolic_flag_and_value_are_resolved(node_script, tmp_path, monkeypatch,
                                                inf_dir):
    """An INF may spell either field as a %token% defined in its own [Strings] section.
    Read as text those raise, the fallback wins, and the INF is silently not being used --
    which is the whole point of reading it."""
    (inf_dir / 'vioscsi.inf').write_text(_INF_WITH_STRINGS)

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0C


def test_a_localised_strings_section_is_read_as_well(node_script, tmp_path, monkeypatch,
                                                     inf_dir):
    (inf_dir / 'vioscsi.inf').write_text(
        _INF_WITH_STRINGS.replace('[Strings]', '[Strings.0409]'))

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0C


def test_a_token_no_strings_section_defines_is_not_guessed_at(node_script, tmp_path,
                                                              monkeypatch, inf_dir):
    (inf_dir / 'vioscsi.inf').write_text(
        _INF_WITH_STRINGS.split('[Strings]')[0])

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A


@pytest.mark.parametrize('flags,name', [
    ('0x000B0001', 'QWORD'),          # FLG_ADDREG_TYPE_QWORD -- shares the low bit
    ('0x00020001', 'NONE'),           # FLG_ADDREG_TYPE_NONE  -- likewise
])
def test_another_type_that_shares_the_dwords_low_bit_is_not_written_as_a_dword(
        node_script, tmp_path, monkeypatch, inf_dir, flags, name):
    """0x00010001 is the DWORD encoding, not the type mask. Matching on it alone lets a
    QWORD entry through, and it would be written back truncated to four bytes -- replacing
    a correct fallback with a value the INF never asked to be a DWORD."""
    (inf_dir / 'vioscsi.inf').write_text(
        _VIOSCSI_INF.replace('"BusType", 0x00010001, 0x0000000A',
                             f'"BusType", {flags}, 0x0000000C'))

    hive = _with_infs(node_script, tmp_path, monkeypatch, inf_dir)
    assert hive.dword(_PARAMS.format('vioscsi'), 'BusType') == 0x0A, \
        f'a {name} entry was adopted as a DWORD'
