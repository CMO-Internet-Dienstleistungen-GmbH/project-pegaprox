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
    # One line per call: the install step is a multi-line argument.
    _stub(bin_dir, 'virt-v2v-in-place', f'''
printf '%s\\n' "v2v $(printf %s "$*" | tr '\\n' ' ')" >> {log}
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
        assert calls[1].startswith('v2v -v --block-driver virtio-scsi --run-command ')
        assert calls[1].endswith(' -i disk -if raw /dev/rbd0')
        assert 'backend=direct' in calls
        assert [c for c in calls if c.startswith('rbd unmap')] == ['rbd unmap /dev/rbd0']
        assert calls[-1] == 'rbd showmapped'
        assert f'{linux.MARK_EXIT}0' in done.stdout

    def test_a_failed_conversion_still_releases_the_device(self, node):
        script = linux.conversion_script([linux.disk_source(RBD_URL)])
        done, calls = node(script, V2V_RC='1')

        assert done.returncode == 1
        assert [c for c in calls if c.startswith('rbd unmap')] == ['rbd unmap /dev/rbd0']
        assert calls[-1] == 'rbd showmapped'
        assert linux.read_result(done.stdout)['exit'] == 1

    def test_a_device_still_busy_is_unmapped_on_a_later_try(self, node, tmp_path):
        """virt-v2v leaves on a signal before nbdkit lets go; the first unmap is EBUSY."""
        busy = tmp_path / 'busy'
        busy.write_text('2')
        script = linux.conversion_script([linux.disk_source(RBD_URL)])
        script = script.replace('sleep 3', 'sleep 0')
        # The stand-in refuses the first two unmaps, as a device nbdkit still holds does.
        wrapper = (f'rbd() {{ if [ "$1" = unmap ] && [ "$(cat {busy})" -gt 0 ]; then '
                   f'echo $(( $(cat {busy}) - 1 )) > {busy}; command rbd "$@" >/dev/null; return 1; fi; '
                   f'command rbd "$@"; }}\n')
        done, calls = node(wrapper + script)

        assert done.returncode == 0
        assert [c for c in calls if c.startswith('rbd unmap')] == ['rbd unmap /dev/rbd0'] * 3
        assert linux.MARK_UNMAP_FAILED not in done.stdout

    def test_a_device_that_stays_mapped_is_reported(self, node):
        script = linux.conversion_script([linux.disk_source(RBD_URL)]).replace('sleep 3', 'sleep 0')
        wrapper = ('rbd() { if [ "$1" = unmap ]; then return 1; fi; '
                   'if [ "$1" = showmapped ]; then echo "0 vm-pool  vm-120-disk-1 - /dev/rbd0"; return 0; fi; '
                   'command rbd "$@"; }\n')
        done, _ = node(wrapper + script)

        assert linux.read_result(done.stdout)['left_mapped'] == ['/dev/rbd0']

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
        assert [c for c in calls if c.startswith('rbd unmap')] == ['rbd unmap /dev/rbd0']
        assert calls[-1] == 'rbd showmapped'

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



class TestTheGuestAgentIsNotConfinedBySelinux:
    """On an enforcing RHEL-family guest the agent runs as virt_qemu_ga_t and may not run
    `ip`, write under /etc or reach NetworkManager. The conversion marks that one domain
    permissive, so guest-exec can do on these guests what it does on every other."""

    def _run(self, tmp_path, config=None, semodule_exit=0):
        """Run the step against a stand-in root: the config and the scratch file moved
        under tmp_path, and a `semodule` that records what it was handed."""
        root = tmp_path / 'root'
        (root / 'tmp').mkdir(parents=True)
        (root / 'etc' / 'selinux').mkdir(parents=True)
        if config is not None:
            (root / 'etc' / 'selinux' / 'config').write_text(config)
        record = tmp_path / 'semodule.calls'
        _stub(tmp_path, 'semodule',
              f'echo "$@" >> {record}; cat "$2" >> {record}; exit {semodule_exit}\n')
        # /tmp/ first: tmp_path itself lies under /tmp on Linux, so replacing it after the
        # config path would rewrite the config path a second time and point it nowhere.
        command = (linux.GUEST_AGENT_SELINUX
                   .replace('/tmp/', f'{root}/tmp/')
                   .replace('/etc/selinux/config', str(root / 'etc/selinux/config')))
        done = subprocess.run(['bash', '-c', command], capture_output=True, text=True,
                              env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}'})
        calls = record.read_text() if record.exists() else ''
        return done, calls, root

    def test_an_enforcing_guest_gets_the_domain_marked_permissive(self, tmp_path):
        done, calls, root = self._run(tmp_path, 'SELINUX=enforcing\nSELINUXTYPE=targeted\n')
        assert done.returncode == 0, done.stderr
        assert f'-i {root}/tmp/{linux.SELINUX_MODULE}.cil' in calls
        assert '(typepermissive virt_qemu_ga_t)' in calls
        assert not (root / 'tmp' / f'{linux.SELINUX_MODULE}.cil').exists()

    def test_a_permissive_guest_gets_it_too(self, tmp_path):
        # Permissive today is enforcing after the next edit of the config.
        _done, calls, _root = self._run(tmp_path, 'SELINUX=permissive\n')
        assert '(typepermissive virt_qemu_ga_t)' in calls

    def test_a_guest_with_selinux_disabled_is_left_alone(self, tmp_path):
        done, calls, _root = self._run(tmp_path, 'SELINUX=disabled\n')
        assert done.returncode == 0 and calls == ''

    def test_a_guest_without_selinux_is_left_alone(self, tmp_path):
        done, calls, _root = self._run(tmp_path, None)
        assert done.returncode == 0 and calls == ''

    def test_a_module_that_cannot_be_installed_fails_the_step(self, tmp_path):
        # A failed step fails virt-v2v, and the migration leaves the VM unstarted rather
        # than delivering a guest whose agent cannot do its work.
        done, _calls, _root = self._run(tmp_path, 'SELINUX=enforcing\n', semodule_exit=1)
        assert done.returncode != 0

    def test_it_is_part_of_every_conversion_and_runs_after_the_unlock(self):
        script = linux.conversion_script([linux.disk_source('/dev/pve/vm-1-disk-0')])
        unlock = script.index('BLACKLIST_RPC=')
        permissive = script.index('typepermissive virt_qemu_ga_t')
        assert unlock < permissive < script.index('-i disk')


# What virt-v2v-in-place 2.6.0 printed on a CentOS 7.9 guest when semodule failed, with the
# appliance's debug chatter left out.
SELINUX_FAILURE_OUTPUT = (
    "[  44.6] Running: if [ -f /etc/selinux/config ] ... semodule -i /tmp/x.cil ...; fi\n"
    "libsemanage.map_file: Unable to open /tmp/x.cil\n"
    " (No such file or directory).\n"
    "semodule:  Failed on /tmp/x.cil!\n"
    "virt-v2v-in-place: error: if [ -f /etc/selinux/config ] && ! grep -qE "
    "'^SELINUX=disabled' /etc/selinux/config; then printf '(typepermissive "
    "virt_qemu_ga_t)\\n' > /tmp/x.cil && semodule -i /tmp/x.cil; fi: command exited "
    "with an error\n"
    "V2V_EXIT=1\n")


def test_a_failed_selinux_step_is_recognised_and_its_reason_kept():
    result = linux.read_result(SELINUX_FAILURE_OUTPUT)
    assert result['exit'] == 1
    assert result['selinux_failed']
    assert any(line.startswith('semodule:  Failed') for line in result['lines'])


def test_another_failure_is_not_blamed_on_selinux():
    result = linux.read_result('virt-v2v-in-place: error: no root device found\nV2V_EXIT=1\n')
    assert result['exit'] == 1 and not result['selinux_failed']

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


# ---------------------------------------------------------------------------
# The guest agent, installed during the conversion
# ---------------------------------------------------------------------------

class TestTheGuestAgentIsInstalled:
    """virt-v2v installs the agent only at first boot, over a network the imported guest
    does not have yet, and never tries again. The conversion installs it itself, over the
    node's network: from the guest's own sources, then from the archive its release moved
    to. Each case runs the real step against a stand-in root, with package managers that
    record what they were asked and succeed only from the source the case names."""

    TOOLS = ('grep', 'sed', 'mktemp', 'mkdir', 'rm', 'tail', 'cat', 'touch', 'cp')

    def _run(self, tmp_path, managers, os_release='', centos_release=None, works_from='own'):
        root = tmp_path / 'root'
        for sub in ('etc/yum.repos.d', 'var/log', 'tmp'):
            (root / sub).mkdir(parents=True)
        (root / 'etc' / 'os-release').write_text(os_release)
        if centos_release is not None:
            (root / 'etc' / 'centos-release').write_text(centos_release + '\n')
        base = tmp_path / 'base'
        base.mkdir()
        for tool in self.TOOLS:
            (base / tool).symlink_to(shutil.which(tool))
        stubs = tmp_path / 'stubs'
        stubs.mkdir()
        calls = tmp_path / 'calls'
        done = root / 'installed'
        archive = 'Dir::Etc::SourceList|--enablerepo=pegaprox-archive'
        for manager in managers:
            _stub(stubs, manager, f'''
echo "{manager} $*" >> {calls}
for a in "$@"; do case "$a" in Dir::Etc::SourceList=*) cp "${{a#*=}}" {root}/seen.list;; esac; done
[ -f {root}/etc/yum.repos.d/pegaprox-archive.repo ] && cp {root}/etc/yum.repos.d/pegaprox-archive.repo {root}/seen.repo
case " $* " in *" install "*) ;; *) exit 0;; esac
if echo "$*" | grep -Eq '{archive}'; then from=archive; else from=own; fi
if [ "$from" = "{works_from}" ]; then touch {done}; exit 0; fi
echo "E: Unable to locate package qemu-guest-agent"; exit 100
''')
        _stub(stubs, 'dpkg-query', f'[ -f {done} ] && echo "install ok installed"\n')
        _stub(stubs, 'rpm', f'[ -f {done} ]\n')
        script = (linux.GUEST_AGENT_INSTALL
                  .replace('/tmp/pegaprox-apt.', f'{root}/tmp/pegaprox-apt.')
                  .replace('/etc/os-release', f'{root}/etc/os-release')
                  .replace('/etc/centos-release', f'{root}/etc/centos-release')
                  .replace('/etc/yum.repos.d/', f'{root}/etc/yum.repos.d/')
                  .replace(linux.AGENT_INSTALL_LOG, f'{root}{linux.AGENT_INSTALL_LOG}'))
        if 'dpkg-query' not in managers and 'apt-get' not in managers:
            (stubs / 'dpkg-query').unlink()
        if not {'dnf', 'yum', 'zypper'} & set(managers):
            (stubs / 'rpm').unlink()
        result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True,
                                env={'PATH': f'{stubs}:{base}'}, timeout=30)
        recorded = calls.read_text().splitlines() if calls.exists() else []
        return result, recorded, root

    def _report(self, result):
        lines = [line for line in result.stdout.splitlines()
                 if line.startswith(linux.MARK_AGENT + ' ')]
        assert len(lines) == 1, result.stdout + result.stderr
        return lines[0][len(linux.MARK_AGENT) + 1:]

    def test_a_guest_that_has_it_is_left_alone(self, tmp_path):
        (tmp_path / 'root').mkdir()
        (tmp_path / 'root' / 'installed').touch()
        result, calls, _ = self._run(tmp_path, ['apt-get'])
        assert self._report(result) == 'present'
        assert calls == []

    def test_the_guests_own_sources_come_first(self, tmp_path):
        result, calls, root = self._run(tmp_path, ['apt-get'],
                                        'ID=ubuntu\nVERSION_CODENAME=focal\n')
        assert self._report(result) == "installed from the guest's package sources"
        assert [c.split()[-1] for c in calls if ' install ' in c] == ['qemu-guest-agent']
        assert not (root / 'seen.list').exists()
        assert all('--no-install-recommends' in c for c in calls if ' install ' in c)

    def test_an_ubuntu_release_out_of_support_comes_from_old_releases(self, tmp_path):
        result, _calls, root = self._run(tmp_path, ['apt-get'],
                                         'ID=ubuntu\nVERSION_CODENAME=kinetic\n',
                                         works_from='archive')
        assert self._report(result) == (
            'installed from the archive http://old-releases.ubuntu.com/ubuntu')
        assert (root / 'seen.list').read_text() == (
            'deb http://old-releases.ubuntu.com/ubuntu kinetic main universe\n'
            'deb http://old-releases.ubuntu.com/ubuntu kinetic-updates main universe\n')
        assert list((root / 'tmp').iterdir()) == []

    def test_a_debian_release_out_of_support_comes_from_the_debian_archive(self, tmp_path):
        result, _calls, root = self._run(tmp_path, ['apt-get'],
                                         'ID=debian\nVERSION_CODENAME=buster\n',
                                         works_from='archive')
        assert self._report(result) == 'installed from the archive http://archive.debian.org/debian'
        assert (root / 'seen.list').read_text() == 'deb http://archive.debian.org/debian buster main\n'

    def test_centos_7_comes_from_the_vault_of_its_own_release(self, tmp_path):
        result, calls, root = self._run(tmp_path, ['yum'], 'ID="centos"\nVERSION_ID="7"\n',
                                        'CentOS Linux release 7.7.1908 (Core)',
                                        works_from='archive')
        assert self._report(result) == 'installed from the archive https://vault.centos.org/7.7.1908'
        repo = (root / 'seen.repo').read_text()
        assert 'baseurl=https://vault.centos.org/7.7.1908/os/x86_64/' in repo
        assert 'baseurl=https://vault.centos.org/7.7.1908/updates/x86_64/' in repo
        assert 'gpgcheck=1' in repo and 'RPM-GPG-KEY-CentOS-7' in repo
        # Nothing it added stays configured, and its metadata does not stay cached.
        assert not (root / 'etc' / 'yum.repos.d' / 'pegaprox-archive.repo').exists()
        assert any(c.startswith('yum clean all') for c in calls)

    def test_centos_8_comes_from_the_vault_and_stream_from_8_stream(self, tmp_path):
        result, _calls, root = self._run(tmp_path, ['dnf'], 'ID="centos"\n',
                                         'CentOS Linux release 8.4.2105', works_from='archive')
        assert self._report(result) == 'installed from the archive https://vault.centos.org/8.4.2105'
        assert 'RPM-GPG-KEY-centosofficial' in (root / 'seen.repo').read_text()

        stream = tmp_path / 'stream'
        stream.mkdir()
        result, _calls, root = self._run(stream, ['dnf'], 'ID="centos"\n',
                                         'CentOS Stream release 8', works_from='archive')
        assert self._report(result) == 'installed from the archive https://vault.centos.org/8-stream'
        assert '/8-stream/AppStream/x86_64/os/' in (root / 'seen.repo').read_text()

    def test_a_release_with_no_archive_reports_why_and_where_the_log_is(self, tmp_path):
        result, _calls, _root = self._run(tmp_path, ['dnf'], 'ID="rocky"\n', works_from='nowhere')
        report = self._report(result)
        assert report.startswith("failed: not installable from the guest's package sources")
        assert 'Unable to locate package' in report
        assert linux.AGENT_INSTALL_LOG in report
        assert result.returncode == 0

    def test_when_the_archive_fails_too_both_are_named(self, tmp_path):
        result, _calls, _root = self._run(tmp_path, ['apt-get'],
                                          'ID=ubuntu\nVERSION_CODENAME=bionic\n',
                                          works_from='nowhere')
        report = self._report(result)
        assert 'and the archive http://old-releases.ubuntu.com/ubuntu' in report
        assert result.returncode == 0

    def test_suse_uses_zypper(self, tmp_path):
        result, calls, _root = self._run(tmp_path, ['zypper'], 'ID="opensuse-leap"\n')
        assert self._report(result) == "installed from the guest's package sources"
        assert calls == ['zypper -n install qemu-guest-agent']

    def test_a_guest_without_a_known_package_manager_still_converts(self, tmp_path):
        result, _calls, _root = self._run(tmp_path, [], 'ID=alpine\n')
        assert self._report(result).startswith('failed: the guest has none of the package managers')
        assert result.returncode == 0

    def test_it_runs_first_so_the_unlock_finds_the_fresh_filter(self):
        """Measured on CentOS 7.9: a freshly installed package writes BLACKLIST_RPC with
        guest-exec in it, so the install has to come before the unlock."""
        script = linux.conversion_script([linux.disk_source('/dev/pve/vm-1-disk-0')])
        install = script.index(linux.AGENT_INSTALL_LOG)
        assert install < script.index('BLACKLIST_RPC=') < script.index('typepermissive')


class TestTheAgentReportReachesTheLog:
    """A command's output appears only in virt-v2v's debug output, on stderr."""

    def test_the_debug_output_is_reduced_to_what_the_log_reads(self, node):
        script = linux.conversion_script([linux.disk_source('/dev/pve/vm-1-disk-0')])
        stub = node.tmp / 'bin' / 'virt-v2v-in-place'
        stub.write_text(f'''#!/bin/bash
echo "[   1.0] Converting"
echo "virt-v2v-in-place: warning: Guestfs.Error(\\"debug: \\") (ignored)"
for i in 1 2; do
  echo "[    0.000000] Linux version 6.12 (appliance kernel)" >&2
  echo "commandrvf: sh -c 'say {linux.MARK_AGENT} present'" >&2
  echo "{linux.MARK_AGENT} installed from the guest's package sources" >&2
done
''')
        before = set(Path('/tmp').glob('pegaprox-v2v-*.log'))
        done, _calls = node(script)
        result = linux.read_result(done.stdout)

        assert result['agent'] == "installed from the guest's package sources"
        assert not result['agent_failed']
        assert result['lines'] == ['[   1.0] Converting']
        assert done.stdout.count(linux.MARK_AGENT) == 1
        assert set(Path('/tmp').glob('pegaprox-v2v-*.log')) == before

    def test_a_conversion_without_a_report_counts_as_a_failed_install(self):
        result = linux.read_result(f'[ 1.0] Converting\n{linux.MARK_EXIT}0\n')
        assert result['agent'] is None and result['agent_failed']

    def test_a_reported_failure_is_one(self):
        result = linux.read_result(f'{linux.MARK_AGENT} failed: no sources\n{linux.MARK_EXIT}0\n')
        assert result['agent'] == 'failed: no sources' and result['agent_failed']

    def test_a_failure_libguestfs_names_reaches_the_log(self, node):
        """Measured twice with parallel conversions: exit 1 after 5 s and not one line on
        stdout. The reason was in the debug output, under a prefix the filter dropped."""
        script = linux.conversion_script([linux.disk_source('/dev/pve/vm-1-disk-0')])
        (node.tmp / 'bin' / 'virt-v2v-in-place').write_text('''#!/bin/bash
echo "[    0.000000] Linux version 6.12 (appliance kernel)" >&2
echo "libguestfs: error: could not create appliance through libvirt" >&2
exit 1
''')
        done, _calls = node(script)
        result = linux.read_result(done.stdout)

        assert result['exit'] == 1
        assert (f'{linux.MARK_DEBUG} libguestfs: error: could not create appliance through '
                'libvirt') in result['lines']
        assert not any('appliance kernel' in line for line in result['lines'])

    def test_a_successful_run_keeps_its_debug_output_to_itself(self, node):
        script = linux.conversion_script([linux.disk_source('/dev/pve/vm-1-disk-0')])
        (node.tmp / 'bin' / 'virt-v2v-in-place').write_text('''#!/bin/bash
echo "libguestfs: trace: everything" >&2
''')
        done, _calls = node(script)
        assert linux.MARK_DEBUG not in done.stdout
