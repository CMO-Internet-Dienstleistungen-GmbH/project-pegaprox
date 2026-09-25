"""What the offline VirtIO injection stages for the guest's first boot.

The driver MSI does not contain the guest agent, and the service that installed the MSI
ran it as a `cmd /c` chain inside its own start, where the SCM holds its database lock.
The injection now stages the agent's own MSI beside the driver MSI, writes firstboot.ps1
next to them, and registers a service that only launches that script. The script itself
is tested under PowerShell in tests/test_virtio_firstboot.py; this file covers the node
side: what the shell script copies and what the registry program registers.
"""
import re
import shutil
import subprocess

import pytest

import pegaprox.core.v2p as v2p
from pegaprox.core.virtio_firstboot import FIRST_BOOT_SCRIPT, FIRST_BOOT_SERVICE_COMMAND


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


def _run_injection(monkeypatch, output=''):
    calls = []
    task = _Task()

    def fake_exec(pve_mgr, node, cmd, timeout=600, **kwargs):
        calls.append(cmd)
        if 'pvesm path' in cmd:
            return 0, '/dev/zvol/tank/vm-100-disk-0\n', ''
        if 'pvesm status' in cmd:
            return 0, 'zfspool\n', ''
        if cmd.startswith('bash /tmp/v2p-virtio-inject-'):
            return 0, output, ''
        return 0, '', ''

    monkeypatch.setattr(v2p, '_pve_node_exec', fake_exec)
    v2p._inject_virtio_drivers(_Manager(), task)
    return next(cmd for cmd in calls if 'NO_NTFS_FOUND' in cmd), task


@pytest.fixture
def node_script(monkeypatch):
    return _run_injection(monkeypatch)[0]


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


def _staging_part(script):
    """The shell lines that copy the installers from the ISO into C:\\qemu."""
    start = script.index('PEGADIR=')
    return script[start:script.index('SYSTEM_HIVE=', start)]


def _stage(script, tmp_path, iso_files):
    iso = tmp_path / 'iso'
    for name in iso_files:
        (iso / name).parent.mkdir(parents=True, exist_ok=True)
        (iso / name).write_bytes(name.encode())
    iso.mkdir(exist_ok=True)
    (tmp_path / 'win' / 'Windows').mkdir(parents=True)
    done = subprocess.run(
        ['bash', '-c', _staging_part(script)], capture_output=True, text=True,
        env={'PATH': '/usr/bin:/bin', 'ISO_MNT': str(iso),
             'WIN_MNT': str(tmp_path / 'win'), 'WDIR': 'Windows'})
    return done.stdout + done.stderr, tmp_path / 'win' / 'qemu'


needs_bash = pytest.mark.skipif(not shutil.which('bash'), reason='no bash')


@needs_bash
def test_the_guest_agent_installer_is_staged_from_the_iso(node_script, tmp_path):
    """virtio-win ships the agent as guest-agent/qemu-ga-x86_64.msi. Checked against
    0.1.302: virtio-win-gt-x64.msi names vioscsi and blnsvr and never qemu-ga."""
    out, staging = _stage(node_script, tmp_path,
                          ['virtio-win-gt-x64.msi', 'guest-agent/qemu-ga-x86_64.msi'])
    assert 'AGENT_STAGED qemu-ga-x86_64.msi' in out, out
    assert (staging / 'qemu-ga-x86_64.msi').read_bytes() == b'guest-agent/qemu-ga-x86_64.msi'


@needs_bash
def test_an_iso_without_the_agent_says_so(node_script, tmp_path):
    out, _ = _stage(node_script, tmp_path, ['virtio-win-gt-x64.msi'])
    assert 'AGENT_MISSING' in out
    assert 'MSI_STAGED virtio-win-gt-x64.msi' in out


@needs_bash
def test_the_first_boot_script_is_staged_with_the_installers(node_script, tmp_path):
    out, staging = _stage(node_script, tmp_path, ['virtio-win-gt-x64.msi'])
    assert 'FIRSTBOOT_STAGED firstboot.ps1' in out, out
    assert (staging / 'firstboot.ps1').read_bytes() == \
        FIRST_BOOT_SCRIPT.replace('\n', '\r\n').encode('utf-8')


@needs_bash
def test_an_x86_only_iso_is_staged_under_the_name_the_script_installs(node_script, tmp_path):
    out, staging = _stage(node_script, tmp_path, ['virtio-win-gt-x86.msi'])
    assert 'MSI_STAGED virtio-win-gt-x86.msi' in out, out
    assert (staging / 'virtio-win-gt-x86.msi').exists()
    assert "'virtio-win-gt-x86.msi'" in FIRST_BOOT_SCRIPT


@needs_bash
def test_without_an_installer_no_first_boot_script_is_staged(node_script, tmp_path):
    out, staging = _stage(node_script, tmp_path, [])
    assert 'MSI_MISSING' in out
    assert not (staging / 'firstboot.ps1').exists()


def _first_boot_command(script):
    service = _embedded_python(script)[1]
    line = next(l for l in service.splitlines() if l.startswith('cmdline = '))
    scope = {}
    exec(line, scope)
    return scope['cmdline']


def test_the_first_boot_service_only_launches_the_staged_script(node_script):
    """The installs run in firstboot.ps1, not in the service's own start, where the SCM
    holds its database lock and the agent's COM registration failed with 0x8007041F."""
    command = _first_boot_command(node_script)
    assert command == FIRST_BOOT_SERVICE_COMMAND
    assert 'msiexec' not in command


def test_the_service_is_registered_only_when_the_script_was_staged(node_script):
    service = _embedded_python(node_script)[1]
    guard = service.index("os.path.join(sys.argv[3], 'firstboot.ps1')")
    assert guard < service.index("set_exp(svc, 'ImagePath', cmdline)")


def test_the_registry_programs_are_valid_python(node_script):
    blocks = _embedded_python(node_script)
    assert len(blocks) == 2
    for block in blocks:
        compile(block, '<injection>', 'exec')


@needs_bash
def test_the_generated_script_is_valid_shell(node_script):
    done = subprocess.run(['bash', '-n'], input=node_script, text=True, capture_output=True)
    assert done.returncode == 0, done.stderr


def test_what_was_staged_reaches_the_migration_log(monkeypatch):
    _, task = _run_injection(monkeypatch, (
        'MSI_STAGED virtio-win-gt-x64.msi\nAGENT_STAGED qemu-ga-x86_64.msi\n'
        'FIRSTBOOT_STAGED firstboot.ps1\nSVC_REGISTERED\nINJECTION_OK\n'))
    assert '[VirtIO] AGENT_STAGED qemu-ga-x86_64.msi' in task.lines
    assert '[VirtIO] FIRSTBOOT_STAGED firstboot.ps1' in task.lines


@needs_bash
def test_the_staging_folder_is_c_qemu(node_script, tmp_path):
    """The installers, the script and the service command all use C:\\qemu."""
    _, staging = _stage(node_script, tmp_path, ['virtio-win-gt-x64.msi'])
    assert (staging / 'virtio-win-gt-x64.msi').read_bytes() == b'virtio-win-gt-x64.msi'
    assert not (tmp_path / 'win' / 'PegaProx').exists()
    assert _first_boot_command(node_script).endswith('-File "C:\\qemu\\firstboot.ps1"')
    assert "$dir = 'C:\\qemu'" in FIRST_BOOT_SCRIPT
