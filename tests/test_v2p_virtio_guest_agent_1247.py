"""#1247 - "Prepare VirtIO drivers" promised the guest agent and did not install it.

The first boot service only ran virtio-win-gt-x64.msi, and a comment assumed that brings qemu-ga along.
It does not: the agent is a package of its own, guest-agent/qemu-ga-x86_64.msi on the same ISO.

The script is the one that reaches the node. The staging part runs in bash against a folder that stands
in for the ISO. It does not run on Windows here.

Fork patch #15 (docs/adr/0007): the staging folder is C:\\qemu, and the first-boot service only launches
firstboot.ps1 (pegaprox/core/virtio_firstboot.py), which waits for Windows Installer before and after each
package instead of for msiexec. Upstream's checks of the service command line are therefore made against
that script: the same promises - the agent after the drivers, both packages waited for, an ISO without the
agent no error, the boot drivers armed, the service and the packages gone afterwards.
"""
import re
import subprocess

import pytest

from pegaprox.core import virtio_firstboot as fb
from test_virtio_injection_node_prereqs_824 import _injection_node_script


def _stage(tmp_path, with_agent):
    iso, win = tmp_path / 'iso', tmp_path / 'win'
    (iso / 'guest-agent').mkdir(parents=True)
    (win / 'Windows').mkdir(parents=True)
    (iso / 'virtio-win-gt-x64.msi').write_text('drivers')
    if with_agent:
        (iso / 'guest-agent' / 'qemu-ga-x86_64.msi').write_text('agent')
    script = _injection_node_script()
    start = script.index('PEGADIR=')
    part = script[start:script.index('SYSTEM_HIVE=', start)]
    out = subprocess.run(['bash', '-c', part], capture_output=True, text=True, timeout=30,
                         env={'PATH': '/usr/bin:/bin', 'WIN_MNT': str(win), 'WDIR': 'Windows', 'ISO_MNT': str(iso)})
    assert out.returncode == 0, out.stderr
    return win / 'qemu', out.stdout


def test_the_agent_package_is_staged_beside_the_drivers(tmp_path):
    folder, out = _stage(tmp_path, True)
    assert (folder / 'qemu-ga-x86_64.msi').read_text() == 'agent'
    assert (folder / 'virtio-win-gt-x64.msi').read_text() == 'drivers'
    assert 'MSI_STAGED qemu-ga-x86_64.msi' in out


def test_an_iso_without_it_still_stages_the_drivers_and_says_so(tmp_path):
    folder, out = _stage(tmp_path, False)
    assert not (folder / 'qemu-ga-x86_64.msi').exists()
    assert (folder / 'virtio-win-gt-x64.msi').exists()
    assert 'GUEST_AGENT_MSI_MISSING' in out


def test_the_first_boot_service_installs_it_after_the_drivers_and_cleans_up():
    script = fb.FIRST_BOOT_SCRIPT
    drivers = script.index("Install-Msi 'virtio-win-gt-x64.msi'")
    agent = script.index("Install-Msi 'qemu-ga-x86_64.msi' 'qemu-ga.log'")
    gone = script.index('& sc.exe delete $service')
    assert drivers < agent < gone, script
    # each install is waited for, and an ISO without the package is no error
    assert 'Start-Process -FilePath \'msiexec.exe\' -ArgumentList $arguments -Wait -PassThru' in script
    assert re.search(r"if \(-not \(Test-Path \$msi\)\) \{\s*Write-Step \(\$name \+ ' not staged, skipped'\)\s*"
                     r"return \$true", script), script
    assert "Remove-Item -Path (Join-Path $dir '*.msi')" in script
    # the service itself only launches that script
    assert fb.FIRST_BOOT_SCRIPT_NAME in fb.FIRST_BOOT_SERVICE_COMMAND
    assert 'msiexec' not in fb.FIRST_BOOT_SERVICE_COMMAND


@pytest.mark.parametrize('needle', ["@('ADDLOCAL=ALL')", 'sc.exe config vioscsi start= boot',
                                    'sc.exe config viostor start= boot'])
def test_what_the_service_did_before_is_still_there(needle):
    assert needle in fb.FIRST_BOOT_SCRIPT
