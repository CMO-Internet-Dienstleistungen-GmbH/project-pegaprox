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
