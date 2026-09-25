"""Which partition the offline VirtIO injection treats as the Windows volume.

It used to mount the largest NTFS partition. A disk with Windows on 100 GB and data on
700 GB had the data partition mounted, found no Windows directory there and ended in
NO_WINDOWS_DIR.

The partition choice is a piece of the shell script handed to the node. These tests cut
it out and run it under bash with stand-ins for blkid, blockdev, mount and umount, so the
real shell logic decides -- against fake partitions, without a block device.
"""
import shutil
import subprocess

import pytest

import pegaprox.core.v2p as v2p


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
    return next(cmd for cmd in calls if 'NO_NTFS_FOUND' in cmd)


def _partition_choice(script):
    """The shell lines that decide which partition is mounted as the Windows volume."""
    start = script.index('NTFS_PARTS=$(')
    end = script.index('echo "WIN_PART=$WIN_PART"', start)
    return script[start:script.index('\n', end) + 1]


def _choose_partition(script, tmp_path, partitions):
    """Run the choice against fake partitions: {name: (size, fstype, has_windows)}.

    `blkid`, `blockdev`, `mount` and `umount` are stand-ins on PATH, and `[ -b ]` answers
    yes for the fake partition files. A "mount" copies the partition's fixture directory
    into the mount point and records its arguments.
    """
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    loop = tmp_path / 'loop0'
    for name, (size, fstype, has_windows) in partitions.items():
        (tmp_path / f'loop0{name}').write_text(f'{size} {fstype}\n')
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


pytestmark = pytest.mark.skipif(not shutil.which('bash'), reason='no bash')


def test_the_windows_partition_is_chosen_over_a_larger_data_partition(node_script, tmp_path):
    """800 GB disk: Windows on 100 GB, data on 700 GB. The largest one is the wrong one."""
    out, _ = _choose_partition(node_script, tmp_path, {
        'p1': (500 * 2**20, 'vfat', False),
        'p2': (100 * 2**30, 'ntfs', True),
        'p3': (700 * 2**30, 'ntfs', False),
    })
    assert f'WIN_PART={tmp_path}/loop0p2' in out, out
    assert 'WINDOWS_PARTITION_NOT_IDENTIFIED' not in out


def test_a_small_recovery_partition_does_not_win_over_windows(node_script, tmp_path):
    """The usual layout: a few hundred MB of recovery NTFS beside the system volume."""
    out, _ = _choose_partition(node_script, tmp_path, {
        'p1': (500 * 2**20, 'ntfs', False),
        'p2': (80 * 2**30, 'ntfs', True),
    })
    assert f'WIN_PART={tmp_path}/loop0p2' in out, out


def test_partitions_are_only_looked_at_read_only_while_choosing(node_script, tmp_path):
    _, mounts = _choose_partition(node_script, tmp_path, {
        'p1': (100 * 2**30, 'ntfs', True),
        'p2': (700 * 2**30, 'ntfs', False),
    })
    assert mounts, 'no candidate was looked at'
    assert all('-o ro ' in line for line in mounts.splitlines()), mounts


def test_the_choice_leaves_nothing_mounted(node_script, tmp_path):
    """The read-write mount further down goes onto the same mount point."""
    _choose_partition(node_script, tmp_path, {
        'p1': (100 * 2**30, 'ntfs', True),
        'p2': (700 * 2**30, 'ntfs', False),
    })
    assert not any((tmp_path / 'win').iterdir())


def test_without_a_recognisable_windows_the_largest_is_kept_and_said(node_script, tmp_path):
    out, _ = _choose_partition(node_script, tmp_path, {
        'p1': (100 * 2**30, 'ntfs', False),
        'p2': (700 * 2**30, 'ntfs', False),
    })
    assert 'WINDOWS_PARTITION_NOT_IDENTIFIED' in out, out
    assert f'WIN_PART={tmp_path}/loop0p2' in out, out


def test_the_operator_sees_when_no_windows_directory_was_found(monkeypatch):
    task = _Task()

    def fake_exec(pve_mgr, node, cmd, timeout=600, **kwargs):
        if 'pvesm path' in cmd:
            return 0, '/dev/zvol/tank/vm-100-disk-0\n', ''
        if 'pvesm status' in cmd:
            return 0, 'zfspool\n', ''
        if cmd.startswith('bash '):
            # What follows it on a real run (the ntfsfix log, NO_WINDOWS_DIR, a listing)
            # is enough to push it out of the 400-character tail logged on failure.
            return 7, ('WINDOWS_PARTITION_NOT_IDENTIFIED\nWIN_PART=/dev/loop0p2\n'
                       + 'x' * 600 + '\nNO_WINDOWS_DIR\n'), ''
        return 0, '', ''

    monkeypatch.setattr(v2p, '_pve_node_exec', fake_exec)
    v2p._inject_virtio_drivers(_Manager(), task)
    assert any(line == '[VirtIO] WINDOWS_PARTITION_NOT_IDENTIFIED' for line in task.lines)


def test_the_generated_script_is_valid_shell(node_script):
    done = subprocess.run(['bash', '-n'], input=node_script, text=True, capture_output=True)
    assert done.returncode == 0, done.stderr


# ── the INF directory is spelled the way the guest spells it ─────────────────
#
# Server 2012 R2 has Windows\Inf, Server 2022 Windows\INF. ntfs-3g is case-sensitive, so
# with a hard-coded INF the .inf files never arrived in a 2012 R2 guest's Inf directory.

def _inf_destination(script):
    start = script.index('INF_DEST=')
    end = script.index('CAT_DEST=', start)
    return script[start:end]


def _resolve_inf_dir(script, tmp_path, existing):
    win = tmp_path / 'mnt' / 'Windows'
    win.mkdir(parents=True)
    if existing:
        (win / existing).mkdir()
    done = subprocess.run(
        ['bash', '-c', _inf_destination(script) + 'printf %s "$INF_DEST"'],
        capture_output=True, text=True,
        env={'PATH': '/usr/bin:/bin', 'WIN_MNT': str(tmp_path / 'mnt'), 'WDIR': 'Windows'})
    return done.stdout


@pytest.mark.parametrize('spelling', ['Inf', 'INF', 'inf'])
def test_the_inf_directory_the_guest_has_is_used(node_script, tmp_path, spelling):
    # Compared as a string: bash's glob returns the name as it is stored, so this holds on
    # a case-insensitive filesystem too, where the old hard-coded INF would also "exist".
    assert _resolve_inf_dir(node_script, tmp_path, spelling) == \
        f'{tmp_path}/mnt/Windows/{spelling}'


def test_a_guest_without_the_directory_gets_the_usual_spelling(node_script, tmp_path):
    assert _resolve_inf_dir(node_script, tmp_path, None) == f'{tmp_path}/mnt/Windows/INF'
