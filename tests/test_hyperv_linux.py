"""The Linux conversion: what it hands the node, and what the node's script really does.

The script is not only read as a string here. It is run, against stand-ins for `rbd` and
`virt-v2v-in-place` that record how they were called, because the parts that matter most
-- that a mapped Ceph device is released on every path, that the libvirt XML names the
devices the mapping produced -- only exist once the shell has expanded it.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from pegaprox.core import hyperv_linux as linux

RBD_URL = ('rbd:vm-pool/vm-120-disk-1:conf=/etc/pve/ceph.conf:id=admin:'
           'keyring=/etc/pve/priv/ceph/vm-pool.keyring')


# ---------------------------------------------------------------------------
# Which path the node opens
# ---------------------------------------------------------------------------

class TestDiskSource:

    def test_an_rbd_url_is_mapped_rather_than_opened(self):
        source = linux.disk_source(RBD_URL)

        assert source == {'kind': 'rbd', 'image': 'vm-pool/vm-120-disk-1', 'id': 'admin',
                          'keyring': '/etc/pve/priv/ceph/vm-pool.keyring',
                          'conf': '/etc/pve/ceph.conf'}

    def test_an_rbd_image_in_a_namespace_keeps_it(self):
        source = linux.disk_source('rbd:pool/ns/vm-1-disk-0:id=admin')
        assert source['image'] == 'pool/ns/vm-1-disk-0'

    @pytest.mark.parametrize('path', ['/dev/pve/vm-120-disk-1',
                                      '/dev/zvol/rpool/data/vm-120-disk-1',
                                      '/dev/rbd-pve/fsid/vm-pool/vm-120-disk-1'])
    def test_a_block_device_is_opened_as_raw(self, path):
        assert linux.disk_source(path) == {'kind': 'block', 'path': path, 'format': 'raw'}

    @pytest.mark.parametrize('name, fmt', [('vm-120-disk-1.raw', 'raw'),
                                           ('vm-120-disk-1.qcow2', 'qcow2')])
    def test_a_file_keeps_its_format(self, name, fmt):
        path = f'/var/lib/vz/images/120/{name}'
        assert linux.disk_source(path) == {'kind': 'file', 'path': path, 'format': fmt}

    @pytest.mark.parametrize('path', ['', 'local:120/vm-120-disk-1.raw',
                                      '/var/lib/vz/images/120/vm-120-disk-1.vmdk',
                                      'rbd:no-pool-here'])
    def test_anything_else_is_refused_rather_than_guessed(self, path):
        with pytest.raises(linux.UnsupportedVolume):
            linux.disk_source(path)


# ---------------------------------------------------------------------------
# The script, run
# ---------------------------------------------------------------------------

def _stub(directory: Path, name: str, body: str):
    path = directory / name
    path.write_text('#!/bin/bash\n' + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def node(tmp_path):
    """A PATH with recording stand-ins for the two tools the script calls."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'calls.log'
    _stub(bin_dir, 'rbd', f'''
echo "rbd $*" >> {log}
if [ "$1" = map ]; then
  n=$(grep -c "^rbd map" {log}); echo "/dev/rbd$((n-1))"
fi
exit ${{RBD_MAP_RC:-0}}
''')
    _stub(bin_dir, 'virt-v2v-in-place', f'''
echo "v2v $*" >> {log}
echo "backend=$LIBGUESTFS_BACKEND" >> {log}
for a in "$@"; do case "$a" in *.xml) cp "$a" {tmp_path}/seen.xml;; esac; done
echo "[   0.0] Setting up the source"
echo "virt-v2v-in-place: This guest requires UEFI on the target to boot."
echo "libguestfs: debug chatter that stays out of the log"
exit ${{V2V_RC:-0}}
''')

    def run(script, **env):
        environment = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', **env}
        done = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                              env=environment, timeout=30)
        calls = log.read_text().splitlines() if log.exists() else []
        return done, calls
    run.tmp = tmp_path
    return run


class TestTheScriptOnOneDisk:

    def test_an_rbd_disk_is_mapped_converted_and_released(self, node):
        script = linux.conversion_script([linux.disk_source(RBD_URL)])
        done, calls = node(script)

        assert done.returncode == 0
        assert calls[0] == ('rbd map -o notrim --id admin --keyring /etc/pve/priv/ceph/vm-pool.keyring '
                            '-c /etc/pve/ceph.conf vm-pool/vm-120-disk-1')
        assert calls[1].startswith('v2v --block-driver virtio-scsi --run-command ')
        assert calls[1].endswith(' -i disk -if raw /dev/rbd0')
        assert 'backend=direct' in calls
        assert calls[-1] == 'rbd unmap /dev/rbd0'
        assert f'{linux.MARK_EXIT}0' in done.stdout

    def test_a_failed_conversion_still_releases_the_device(self, node):
        script = linux.conversion_script([linux.disk_source(RBD_URL)])
        done, calls = node(script, V2V_RC='1')

        assert done.returncode == 1
        assert calls[-1] == 'rbd unmap /dev/rbd0'
        assert linux.read_result(done.stdout)['exit'] == 1

    def test_a_disk_that_cannot_be_mapped_is_not_converted(self, node):
        script = linux.conversion_script([linux.disk_source(RBD_URL)])
        done, calls = node(script, RBD_MAP_RC='1')

        assert done.returncode == 3
        assert not any(call.startswith('v2v') for call in calls)
        result = linux.read_result(done.stdout)
        assert result['map_failed'] and result['exit'] is None

    def test_a_block_device_is_converted_in_place_without_mapping(self, node):
        script = linux.conversion_script([linux.disk_source('/dev/pve/vm-120-disk-1')])
        done, calls = node(script)

        assert done.returncode == 0
        assert not any(call.startswith('rbd') for call in calls)
        assert calls[0].endswith(' -i disk -if raw /dev/pve/vm-120-disk-1')

    def test_a_qcow2_file_is_named_as_qcow2(self, node):
        path = '/var/lib/vz/images/120/vm-120-disk-1.qcow2'
        done, calls = node(linux.conversion_script([linux.disk_source(path)]))

        assert calls[0].endswith(f' -i disk -if qcow2 {path}')


class TestTheScriptOnSeveralDisks:
    """`-i disk` takes one disk; a guest whose root spans two needs all of them at once."""

    def test_every_disk_goes_into_one_libvirt_description(self, node):
        sources = [linux.disk_source(RBD_URL),
                   linux.disk_source('/dev/pve/vm-120-disk-2'),
                   linux.disk_source('/var/lib/vz/images/120/vm-120-disk-3.qcow2')]
        done, calls = node(linux.conversion_script(sources, guest_name='vm-120'))

        assert done.returncode == 0
        v2v = next(call for call in calls if call.startswith('v2v'))
        assert ' -i libvirtxml ' in v2v
        xml = (node.tmp / 'seen.xml').read_text()
        assert "<name>vm-120</name>" in xml
        assert '<source dev="/dev/rbd0"/><target dev=\'sda\'' in xml
        assert '<source dev="/dev/pve/vm-120-disk-2"/><target dev=\'sdb\'' in xml
        assert ("<driver name='qemu' type='qcow2'/>"
                '<source file="/var/lib/vz/images/120/vm-120-disk-3.qcow2"/>') in xml
        assert calls[-1] == 'rbd unmap /dev/rbd0'

    def test_the_description_does_not_outlive_the_run(self, node, tmp_path):
        sources = [linux.disk_source('/dev/pve/vm-120-disk-1'),
                   linux.disk_source('/dev/pve/vm-120-disk-2')]
        before = set(Path('/tmp').glob('pegaprox-v2v-*.xml'))
        node(linux.conversion_script(sources))
        assert set(Path('/tmp').glob('pegaprox-v2v-*.xml')) == before

    def test_nothing_is_refused_that_the_script_cannot_name(self):
        with pytest.raises(linux.UnsupportedVolume):
            linux.conversion_script([])


# ---------------------------------------------------------------------------
# guest-exec, and the rest of what the node is asked
# ---------------------------------------------------------------------------

class TestTheGuestAgentIsUnlocked:
    """Every guest here runs with guest-exec; RHEL-family packages switch it off."""

    def _run_on(self, tmp_path, content):
        """Run the edit with GNU sed, which is what the guest has. BSD sed reads `-i -e`
        as an in-place suffix, so on a Mac the test goes through `gsed` or not at all."""
        gnu = shutil.which('gsed') or (shutil.which('sed') if subprocess.run(
            ['sed', '--version'], capture_output=True).returncode == 0 else None)
        if not gnu:
            pytest.skip('GNU sed is not available')
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'sed').symlink_to(gnu)
        config = tmp_path / 'qemu-ga'
        config.write_text(content)
        command = linux.GUEST_AGENT_UNLOCK.replace('/etc/sysconfig/qemu-ga', str(config))
        subprocess.run(['bash', '-c', command], check=True,
                       env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'})
        return config.read_text()

    def test_the_rhel7_blacklist_is_emptied(self, tmp_path):
        text = self._run_on(tmp_path, 'BLACKLIST_RPC=guest-file-open,guest-exec,'
                                      'guest-exec-status\nFSFREEZE_HOOK_PATHNAME=/x\n')
        assert text == 'BLACKLIST_RPC=\nFSFREEZE_HOOK_PATHNAME=/x\n'

    def test_the_later_filter_is_emptied(self, tmp_path):
        text = self._run_on(tmp_path, 'FILTER_RPC_ARGS="--allow-rpcs=guest-ping"\n')
        assert text == 'FILTER_RPC_ARGS=\n'

    def test_a_guest_without_the_file_is_left_alone(self, tmp_path):
        command = linux.GUEST_AGENT_UNLOCK.replace('/etc/sysconfig/qemu-ga',
                                                   str(tmp_path / 'absent'))
        assert subprocess.run(['bash', '-c', command]).returncode == 0
        assert not (tmp_path / 'absent').exists()

    def test_it_is_part_of_every_conversion(self):
        script = linux.conversion_script([linux.disk_source('/dev/pve/vm-1-disk-0')])
        assert '--run-command' in script and 'BLACKLIST_RPC=' in script


def test_the_install_reports_apts_own_failure(tmp_path):
    """`apt-get ... | tail` reports tail's success for an install that failed."""
    _stub(tmp_path, 'apt-get', 'echo "E: Unable to locate package"; exit 100\n')
    done = subprocess.run(['bash', '-c', linux.INSTALL_COMMAND], capture_output=True,
                          text=True, env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}'})
    assert done.returncode == 100
    assert 'Unable to locate package' in done.stdout


def test_the_install_never_pulls_recommends():
    """supermin recommends a Debian kernel image, which a PVE node must not be given."""
    assert '--no-install-recommends' in linux.INSTALL_COMMAND
    assert 'libguestfs-xfs' in linux.INSTALL_COMMAND


def test_the_log_keeps_the_steps_and_drops_the_chatter():
    result = linux.read_result('[   0.0] Setting up\nlibguestfs: noise\n'
                               'virt-v2v-in-place: This guest requires UEFI on the target '
                               f'to boot.\n{linux.MARK_EXIT}0\n')
    assert result['lines'] == ['[   0.0] Setting up',
                               'virt-v2v-in-place: This guest requires UEFI on the target '
                               'to boot.']
    assert result['exit'] == 0 and result['uefi']
