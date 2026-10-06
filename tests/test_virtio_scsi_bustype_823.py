"""#823 - the offline VirtIO injection wrote the wrong storage registry for vioscsi.

Checked against vioscsi.inf / viostor.inf on the current virtio-win ISO:

  1. Both services got Parameters\\BusType = 1. viostor.inf writes 1, vioscsi.inf writes
     0x0A. With 1 a Windows guest whose boot disk sits on virtio-scsi (the Proxmox default
     scsihw) stops with INACCESSIBLE_BOOT_DEVICE. Both INFs also write
     Parameters\\DmaRemappingCompatible = 0, which we did not write at all.
  2. The CriticalDeviceDatabase mapped pci#ven_1af4&dev_1041 to vioscsi. 1041 is
     virtio-net. The modern ids (1042 blk, 1048 scsi) were missing, and those are what a
     guest sees on a machine type that exposes modern-only devices.

These tests do not grep the source. They pull the hivex script out of the node script
_inject_virtio_drivers builds, run it the way the node does (python3 - SYSTEM), with a
fake hivex module that records every key and value, and check what was written.
MK
"""
import ast
import json
import pathlib
import subprocess
import sys

import pytest

_V2P = pathlib.Path(__file__).resolve().parent.parent / 'pegaprox' / 'core' / 'v2p.py'

_REG_SZ, _REG_EXPAND_SZ, _REG_DWORD = 1, 2, 4
_SCSI_ADAPTER_CLASS = '{4D36E97B-E325-11CE-BFC1-08002BE10318}'

# Hardware ids exactly as the INFs list them (transitional first, then modern).
_INF_IDS = {
    'viostor': [
        r'PCI\VEN_1AF4&DEV_1001&SUBSYS_00021AF4&REV_00',
        r'PCI\VEN_1AF4&DEV_1001',
        r'PCI\VEN_1AF4&DEV_1042&SUBSYS_11001AF4&REV_01',
        r'PCI\VEN_1AF4&DEV_1042',
    ],
    'vioscsi': [
        r'PCI\VEN_1AF4&DEV_1004&SUBSYS_00081AF4&REV_00',
        r'PCI\VEN_1AF4&DEV_1004',
        r'PCI\VEN_1AF4&DEV_1048&SUBSYS_11001AF4&REV_01',
        r'PCI\VEN_1AF4&DEV_1048',
    ],
}
# Plus the VEN&DEV&SUBSYS form in between, which Windows also matches on.
_SUBSYS_IDS = {
    'viostor': [r'PCI\VEN_1AF4&DEV_1001&SUBSYS_00021AF4',
                r'PCI\VEN_1AF4&DEV_1042&SUBSYS_11001AF4'],
    'vioscsi': [r'PCI\VEN_1AF4&DEV_1004&SUBSYS_00081AF4',
                r'PCI\VEN_1AF4&DEV_1048&SUBSYS_11001AF4'],
}


def _cdb_key(hwid):
    """PCI\\VEN_1AF4&DEV_1001 -> pci#ven_1af4&dev_1001, the CriticalDeviceDatabase key form."""
    return hwid.replace('\\', '#').lower()


def _expected_keys(driver):
    return {_cdb_key(i) for i in _INF_IDS[driver] + _SUBSYS_IDS[driver]}


# ── recover the hivex script the node actually runs ──────────────────────────

def _injection_node_script():
    tree = ast.parse(_V2P.read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_inject_virtio_drivers')

    def flatten(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value
        if isinstance(n, ast.JoinedStr):
            return ''.join(v.value if isinstance(v, ast.Constant) else 'PLACEHOLDER'
                           for v in n.values)
        if isinstance(n, ast.BinOp):
            return flatten(n.left) + flatten(n.right)
        return ''

    best = ''
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            txt = flatten(node.value)
            if 'WDIR=' in txt and len(txt) > len(best):
                best = txt
    assert best, "could not recover the node script from _inject_virtio_drivers"
    return best


def _storage_hivex_script():
    """Body of the `python3 - "$SYSTEM_HIVE" << 'PYEOF'` heredoc, verbatim."""
    script = _injection_node_script()
    opener = "python3 - \"$SYSTEM_HIVE\" << 'PYEOF'"
    start = script.find(opener)
    assert start != -1, "the storage registry merge is gone from the node script"
    body_start = script.index('\n', start) + 1
    end = script.find('\nPYEOF\n', body_start)
    assert end != -1, "PYEOF heredoc is not terminated"
    return script[body_start:end + 1]


_FAKE_HIVEX = '''
import json, os

class Hivex:
    def __init__(self, path, write=False):
        self._nodes = {0: {'children': {}, 'values': {}}}
        self._next = 1

    def root(self):
        return 0

    def node_get_child(self, node, name):
        return self._nodes[node]['children'].get(name)

    def node_add_child(self, node, name):
        nid = self._next
        self._next += 1
        self._nodes[nid] = {'children': {}, 'values': {}}
        self._nodes[node]['children'][name] = nid
        return nid

    def node_set_value(self, node, val):
        self._nodes[node]['values'][val['key']] = [val['t'], val['value'].hex()]

    def commit(self, path):
        def dump(nid):
            n = self._nodes[nid]
            return {'values': n['values'],
                    'children': {k: dump(c) for k, c in n['children'].items()}}
        with open(os.environ['FAKE_HIVE_OUT'], 'w') as f:
            json.dump(dump(0), f)
'''


def _run_merge(tmp_path, drivers):
    """Run the storage merge against a fake SYSTEM hive with `drivers` staged."""
    fake = tmp_path / 'fakemods' / 'hivex'
    fake.mkdir(parents=True)
    (fake / '__init__.py').write_text(_FAKE_HIVEX)
    (fake / 'hive_types.py').write_text(
        f'REG_SZ = {_REG_SZ}\nREG_EXPAND_SZ = {_REG_EXPAND_SZ}\nREG_DWORD = {_REG_DWORD}\n')

    win = tmp_path / 'win' / 'Windows' / 'System32'
    (win / 'config').mkdir(parents=True)
    (win / 'drivers').mkdir()
    hive = win / 'config' / 'SYSTEM'
    hive.write_bytes(b'')
    for d in drivers:
        (win / 'drivers' / f'{d}.sys').write_bytes(b'MZ')

    out = tmp_path / 'hive.json'
    p = subprocess.run(
        [sys.executable, '-', str(hive)], input=_storage_hivex_script(), text=True,
        capture_output=True, timeout=60,
        env={'PYTHONPATH': str(tmp_path / 'fakemods'), 'FAKE_HIVE_OUT': str(out),
             'PATH': '/usr/bin:/bin'})
    assert p.returncode == 0, f"the hivex merge failed:\n{p.stdout}\n{p.stderr}"
    assert 'hivex commit OK' in p.stdout
    return json.loads(out.read_text())


def _child(tree, *path):
    n = tree
    for part in path:
        assert part in n['children'], f"missing key {'/'.join(path)}"
        n = n['children'][part]
    return n


def _dword(node, name):
    assert name in node['values'], f"value {name} not written"
    t, raw = node['values'][name]
    assert t == _REG_DWORD, f"{name} is not a REG_DWORD"
    b = bytes.fromhex(raw)
    assert len(b) == 4, f"{name} is not 4 bytes"
    return int.from_bytes(b, 'little')


def _sz(node, name):
    t, raw = node['values'][name]
    assert t == _REG_SZ
    return bytes.fromhex(raw).decode('utf-16-le').rstrip('\x00')


def _services(tree):
    return _child(tree, 'ControlSet001', 'Services')


def _cdb(tree):
    return _child(tree, 'ControlSet001', 'Control', 'CriticalDeviceDatabase')['children']


# ── 1) per-driver Parameters, as the INF writes them ─────────────────────────

def test_vioscsi_gets_bus_type_sas_not_scsi(tmp_path):
    tree = _run_merge(tmp_path, ['viostor', 'vioscsi'])
    params = _child(_services(tree), 'vioscsi', 'Parameters')
    assert _dword(params, 'BusType') == 0x0A, (
        "vioscsi.inf writes BusType 0x0A; with 1 a guest booting from virtio-scsi "
        "stops at INACCESSIBLE_BOOT_DEVICE")


def test_viostor_keeps_bus_type_one(tmp_path):
    tree = _run_merge(tmp_path, ['viostor', 'vioscsi'])
    params = _child(_services(tree), 'viostor', 'Parameters')
    assert _dword(params, 'BusType') == 0x01


@pytest.mark.parametrize('driver', ['viostor', 'vioscsi'])
def test_dma_remapping_compatible_is_zero_for_both(tmp_path, driver):
    tree = _run_merge(tmp_path, ['viostor', 'vioscsi'])
    params = _child(_services(tree), driver, 'Parameters')
    assert _dword(params, 'DmaRemappingCompatible') == 0


@pytest.mark.parametrize('driver', ['viostor', 'vioscsi'])
def test_the_rest_of_the_service_entry_is_unchanged(tmp_path, driver):
    """Guard: the BusType fix must not lose the values that make it a boot-start miniport."""
    tree = _run_merge(tmp_path, ['viostor', 'vioscsi'])
    svc = _child(_services(tree), driver)
    assert _dword(svc, 'Start') == 0
    assert _dword(svc, 'Type') == 1
    assert _sz(svc, 'Group') == 'SCSI miniport'
    assert _dword(_child(svc, 'Parameters', 'PnpInterface'), '5') == 1


# ── 2) CriticalDeviceDatabase carries the ids the INFs name ──────────────────

def test_both_drivers_get_every_inf_id_and_nothing_else(tmp_path):
    cdb = _cdb(_run_merge(tmp_path, ['viostor', 'vioscsi']))
    want = _expected_keys('viostor') | _expected_keys('vioscsi')
    assert set(cdb) == want, (
        f"missing: {sorted(want - set(cdb))}  unexpected: {sorted(set(cdb) - want)}")


@pytest.mark.parametrize('driver', ['viostor', 'vioscsi'])
def test_each_id_points_at_its_own_driver_and_the_scsi_adapter_class(tmp_path, driver):
    cdb = _cdb(_run_merge(tmp_path, ['viostor', 'vioscsi']))
    for key in _expected_keys(driver):
        assert key in cdb, f"{key} not registered"
        assert _sz(cdb[key], 'Service') == driver, f"{key} names the wrong service"
        assert _sz(cdb[key], 'ClassGUID') == _SCSI_ADAPTER_CLASS


@pytest.mark.parametrize('driver', ['viostor', 'vioscsi'])
def test_the_modern_device_id_is_registered(tmp_path, driver):
    """On a machine type with modern-only devices the guest sees 1042/1048, not 1001/1004."""
    cdb = _cdb(_run_merge(tmp_path, ['viostor', 'vioscsi']))
    modern = {'viostor': 'dev_1042', 'vioscsi': 'dev_1048'}[driver]
    assert any(modern in k and _sz(v, 'Service') == driver for k, v in cdb.items()), \
        f"no {modern} entry for {driver}"


def test_virtio_net_is_not_mapped_to_a_storage_driver(tmp_path):
    cdb = _cdb(_run_merge(tmp_path, ['viostor', 'vioscsi']))
    assert not [k for k in cdb if 'dev_1041' in k], \
        "dev_1041 is virtio-net (netkvm), not a storage controller"


# ── 3) a guest gets only what was actually staged ────────────────────────────

@pytest.mark.parametrize('present,absent', [('viostor', 'vioscsi'), ('vioscsi', 'viostor')])
def test_a_guest_with_one_driver_gets_only_its_entries(tmp_path, present, absent):
    tree = _run_merge(tmp_path, [present])
    assert absent not in _services(tree)['children'], (
        f"{absent} registered as boot-start without its .sys - that alone is a "
        "boot failure")
    cdb = _cdb(tree)
    assert set(cdb) == _expected_keys(present), (
        f"missing: {sorted(_expected_keys(present) - set(cdb))}  "
        f"unexpected: {sorted(set(cdb) - _expected_keys(present))}")
    assert all(_sz(v, 'Service') == present for v in cdb.values())


def test_no_storage_driver_staged_writes_no_storage_entries(tmp_path):
    tree = _run_merge(tmp_path, [])
    assert not ({'viostor', 'vioscsi'} & set(_services(tree)['children']))
    assert _cdb(tree) == {}


def test_the_merge_script_is_valid_python():
    compile(_storage_hivex_script(), '<pyeof>', 'exec')
