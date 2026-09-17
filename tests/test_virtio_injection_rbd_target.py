# VirtIO driver injection onto a Ceph RBD target.
#
# The injection script's `rbd)` branch took pool and image from the PVE volume id
# (`<storage>:vm-<vmid>-disk-0`), which names the storage and carries no pool. After
# `cut -d/` both came out as the image name, so every injection onto Ceph ended in
# `rbd: error opening pool 'vm-100-disk-0'` and the guest booted without drivers.
#
# These tests take the script exactly as `_inject_virtio_drivers` sends it to the node,
# cut out the `rbd)` branch and run it under bash with a stand-in `rbd` that records how
# it was called. What reaches `rbd map` is the thing that was wrong, so that is what is
# checked, not the Python that builds it.

import os
import re
import shlex
import stat
import subprocess
import textwrap
import types

import pytest

import pegaprox.core.v2p as v2p

URI_WITH_OPTIONS = ('rbd:rbdpool/vm-100-disk-0:conf=/etc/pve/ceph.conf:id=admin'
                    ':keyring=/etc/pve/priv/ceph/ceph-vms.keyring')


def _script_for(monkeypatch, vol_path, storage='ceph-vms', storage_type='rbd'):
    """The injection script `_inject_virtio_drivers` writes to the node for this disk."""
    sent = {}

    def fake_exec(_mgr, _node, cmd, timeout=None):
        if cmd.startswith('cat > '):
            sent['script'] = cmd.split("<< 'EOFSCRIPT'\n", 1)[1].rsplit('EOFSCRIPT', 1)[0]
            return 0, '', ''
        if cmd.startswith('pvesm path'):
            return 0, vol_path + '\n', ''
        if cmd.startswith('pvesm status'):
            return 0, storage_type + '\n', ''
        if cmd.startswith('bash /tmp/v2p-virtio-inject-'):
            return 2, 'not run in this test', ''
        return 0, '', ''

    monkeypatch.setattr(v2p, '_pve_node_exec', fake_exec)
    mgr = types.SimpleNamespace(host='127.0.0.1', api_port=8006,
                                _api_get=lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    task = types.SimpleNamespace(install_virtio_drivers=True, target_node='node1',
                                 proxmox_vmid=100, target_storage=storage,
                                 virtio_iso_path='/iso/virtio-win.iso', ostype='',
                                 config={}, log=lambda *_a: None)
    v2p._inject_virtio_drivers(mgr, task)
    return sent['script']


def _rbd_branch(script):
    match = re.search(r'\n  rbd\)\n(.*?)\n    ;;\n', script, re.S)
    assert match, 'the injection script has no rbd) branch'
    return match.group(1)


def _run_branch(tmp_path, branch, vol, rbd_prints='/nonexistent/rbd0'):
    """Run the rbd) branch under bash; return (exit code, output, recorded rbd calls, RBD)."""
    calls = tmp_path / 'rbd-calls'
    stub = tmp_path / 'bin' / 'rbd'
    stub.parent.mkdir()
    stub.write_text(f'#!/bin/bash\necho "$*" >> {calls}\necho {rbd_prints}\n')
    stub.chmod(0o755)
    body = textwrap.dedent(f'''\
        set -u
        VOL={shlex.quote(vol)}
        VOL_ID='ceph-vms:vm-100-disk-0'
        BLK=""; RBD=""
        run() {{
        {branch}
        }}
        run
        echo "BLK=$BLK RBD=$RBD"
        ''')
    env = dict(os.environ, PATH=f'{stub.parent}:{os.environ["PATH"]}')
    done = subprocess.run(['bash', '-c', body], capture_output=True, text=True, env=env,
                          cwd=tmp_path)
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return done.returncode, done.stdout + done.stderr, recorded


def _any_block_device():
    for name in sorted(os.listdir('/dev')):
        path = os.path.join('/dev', name)
        try:
            if stat.S_ISBLK(os.stat(path).st_mode):
                return path
        except OSError:
            continue
    return None


def test_a_librbd_uri_is_mapped_with_its_pool_image_and_options(monkeypatch, tmp_path):
    branch = _rbd_branch(_script_for(monkeypatch, URI_WITH_OPTIONS))
    _, _, calls = _run_branch(tmp_path, branch, URI_WITH_OPTIONS)
    assert calls == ['map --id admin --conf /etc/pve/ceph.conf '
                     '--keyring /etc/pve/priv/ceph/ceph-vms.keyring -p rbdpool vm-100-disk-0']


def test_the_pool_comes_from_pvesm_path_never_from_the_storage_id_or_the_image(
        monkeypatch, tmp_path):
    """The storage is called `ceph-vms`, the Ceph pool `rbdpool`. The old branch mapped
    `-p vm-100-disk-0 vm-100-disk-0`; neither of the wrong names may reach `rbd map`."""
    branch = _rbd_branch(_script_for(monkeypatch, URI_WITH_OPTIONS, storage='ceph-vms'))
    _, _, calls = _run_branch(tmp_path, branch, URI_WITH_OPTIONS)
    assert len(calls) == 1
    assert ' -p rbdpool ' in f' {calls[0]} '
    assert '-p ceph-vms' not in calls[0]
    assert '-p vm-100-disk-0' not in calls[0]


def test_a_failed_map_is_reported_and_stops_the_script(monkeypatch, tmp_path):
    branch = _rbd_branch(_script_for(monkeypatch, URI_WITH_OPTIONS))
    code, output, _ = _run_branch(tmp_path, branch, URI_WITH_OPTIONS,
                                  rbd_prints='rbd: sysfs write failed')
    assert code == 2
    assert 'RBD_MAP_FAILED: rbd: sysfs write failed' in output


def test_a_device_the_script_mapped_itself_is_unmapped_again(monkeypatch, tmp_path):
    """The cleanup unmaps whatever RBD names, so a successful map must set it."""
    device = _any_block_device()
    if not device:
        pytest.skip('no block device on this machine to stand in for /dev/rbdN')
    branch = _rbd_branch(_script_for(monkeypatch, URI_WITH_OPTIONS))
    code, output, _ = _run_branch(tmp_path, branch, URI_WITH_OPTIONS, rbd_prints=device)
    assert code == 0, output
    assert f'BLK={device} RBD={device}' in output


def test_an_existing_krbd_device_is_used_as_it_is_and_left_mapped(monkeypatch, tmp_path):
    """On a krbd storage PVE maps the image itself. Mapping it again is unnecessary, and
    unmapping it in the cleanup would pull the disk out from under PVE."""
    device = _any_block_device()
    if not device:
        pytest.skip('no block device on this machine to stand in for /dev/rbd-pve/...')
    krbd_path = '/dev/rbd-pve/fsid/rbdpool/vm-100-disk-0'
    branch = _rbd_branch(_script_for(monkeypatch, krbd_path))
    code, output, calls = _run_branch(tmp_path, branch, device)
    assert code == 0, output
    assert calls == []
    assert f'BLK={device} RBD=' + '\n' in output + '\n'
    assert f'RBD={device}' not in output


def test_a_krbd_path_that_is_not_mapped_yet_is_mapped_from_the_path(monkeypatch, tmp_path):
    krbd_path = '/dev/rbd-pve/fsid/rbdpool/vm-100-disk-0'
    branch = _rbd_branch(_script_for(monkeypatch, krbd_path))
    _, _, calls = _run_branch(tmp_path, branch, '/nonexistent/rbd-pve/path')
    assert calls == ['map -p rbdpool vm-100-disk-0']


def test_a_uri_without_pool_and_image_stops_before_rbd_map(monkeypatch, tmp_path):
    uri = 'rbd:vm-100-disk-0:conf=/etc/pve/ceph.conf'
    branch = _rbd_branch(_script_for(monkeypatch, uri))
    code, output, calls = _run_branch(tmp_path, branch, uri)
    assert code == 2
    assert 'RBD_PARSE_FAILED' in output
    assert calls == []


def test_the_pool_is_no_longer_parsed_out_of_the_volume_id(monkeypatch):
    branch = _rbd_branch(_script_for(monkeypatch, URI_WITH_OPTIONS))
    assert 'VOL_ID' not in branch
    assert 'cut -d/' not in branch


def test_a_hostile_uri_cannot_break_out_of_the_map_command(monkeypatch, tmp_path):
    uri = "rbd:pool/vm-100-disk-0$(touch pwned):id=a'b;touch pwned"
    branch = _rbd_branch(_script_for(monkeypatch, uri))
    _run_branch(tmp_path, branch, uri)
    assert not (tmp_path / 'pwned').exists()
