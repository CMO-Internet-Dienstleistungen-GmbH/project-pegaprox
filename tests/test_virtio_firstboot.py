"""The first-boot install that follows an offline VirtIO injection.

On the guest, `start "" /wait msiexec` did not keep the driver MSI and the agent MSI
apart: both logs start two to three seconds after each other and overlap, and which of
the two fails varies. The script is therefore run here under PowerShell against a fake
msiexec that behaves the way the guest did -- it returns while its installation is still
executing and holding Windows Installer's `Global\\_MSIExecute` mutex -- and the order
in which the installations actually ran is read back.

Skipped where no `pwsh` is installed.
"""
import base64
import shutil
import subprocess
import textwrap

import pytest

from pegaprox.core import virtio_firstboot as fb

PWSH = shutil.which('pwsh')
needs_pwsh = pytest.mark.skipif(PWSH is None, reason='pwsh is not installed')

# How long one fake installation executes after its msiexec has returned.
FAKE_INSTALL_SECONDS = 3


def _fake_msiexec(tmp_path, exit_codes, fail_always=False):
    """An executable that starts a detached 'installation' and returns before it ends.

    The installation takes the installer mutex, records begin and end, and releases it.
    msiexec itself waits only until the mutex is held, then exits with the code given for
    that MSI -- a failing one fails once, then succeeds, so a retry can be seen, or fails
    every time with `fail_always`.
    """
    events = tmp_path / 'events.log'
    holder = tmp_path / 'install.ps1'
    holder.write_text(textwrap.dedent(f'''\
        param($name)
        $m = New-Object System.Threading.Mutex($false, 'Global\\_MSIExecute')
        $null = $m.WaitOne()
        Add-Content -Path '{events}' -Value ('begin ' + $name)
        Start-Sleep -Seconds {FAKE_INSTALL_SECONDS}
        Add-Content -Path '{events}' -Value ('end ' + $name)
        $m.ReleaseMutex(); $m.Dispose()
        '''))
    failing = ', '.join(f"'{name}'" for name, code in exit_codes.items() if code)
    fail_code = max(exit_codes.values(), default=0)
    impl = tmp_path / 'msiexec.ps1'
    impl.write_text(textwrap.dedent(f'''\
        $name = Split-Path -Leaf ($args[1].Trim('"'))
        Add-Content -Path '{events}' -Value ('invoked ' + $name)
        Start-Process -FilePath '{PWSH}' -ArgumentList @('-NoProfile', '-File', '{holder}', $name)
        do {{
            Start-Sleep -Milliseconds 100
            try {{ ([System.Threading.Mutex]::OpenExisting('Global\\_MSIExecute')).Close(); $held = $true }}
            catch {{ $held = $false }}
        }} until ($held)
        $failOnce = @({failing})
        $marker = '{tmp_path}/failed-once-' + $name
        if (($failOnce -contains $name) -and ({'$true' if fail_always else '$false'} -or -not (Test-Path $marker))) {{
            Set-Content -Path $marker -Value ''
            exit {fail_code}
        }}
        exit 0
        '''))
    # A shell wrapper, because pwsh is itself a script on some installs and the kernel
    # does not accept a script as a shebang interpreter.
    msiexec = tmp_path / 'msiexec'
    msiexec.write_text(f'#!/bin/bash\nexec "{PWSH}" -NoProfile -File "{impl}" "$@"\n')
    msiexec.chmod(0o755)
    return msiexec, events


def _run_script(tmp_path, exit_codes=None, wait_for_installer=True, fail_always=False):
    """Run firstboot.ps1 with its Windows paths pointed into tmp_path."""
    staging = tmp_path / 'qemu'
    staging.mkdir()
    for name in ('virtio-win-gt-x64.msi', 'qemu-ga-x86_64.msi'):
        (staging / name).write_bytes(b'msi')
    msiexec, events = _fake_msiexec(tmp_path, exit_codes or {}, fail_always)
    sc_log = tmp_path / 'sc.log'
    sc = tmp_path / 'sc'
    sc.write_text(f'#!/bin/bash\necho "$*" >> "{sc_log}"\n')
    sc.chmod(0o755)
    script = (fb.FIRST_BOOT_SCRIPT
              .replace("$dir = 'C:\\qemu'", f"$dir = '{staging}'")
              .replace("Join-Path $env:SystemRoot 'Temp\\pegaprox-firstboot.log'",
                       f"'{tmp_path / 'firstboot.log'}'")
              .replace("'msiexec.exe'", f"'{msiexec}'")
              .replace('& sc.exe ', f"& '{sc}' ")
              .replace('Get-Service -Name $service -ErrorAction SilentlyContinue', '$null')
              .replace("Set-Location -Path 'C:\\'", f"Set-Location -Path '{tmp_path}'"))
    if not wait_for_installer:
        script = script.replace('while (Test-InstallerBusy)', 'while ($false)')
    path = tmp_path / 'firstboot.ps1'
    path.write_text(script)
    done = subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-File', str(path)],
                          capture_output=True, text=True, timeout=120)
    return done, events.read_text().splitlines(), staging


def _order(events):
    return {line: i for i, line in reversed(list(enumerate(events)))}


@needs_pwsh
def test_the_agent_starts_only_after_the_driver_installation_has_ended(tmp_path):
    done, events, _ = _run_script(tmp_path)

    at = _order(events)
    assert done.returncode == 0, done.stdout + done.stderr
    assert at['end virtio-win-gt-x64.msi'] < at['invoked qemu-ga-x86_64.msi'], events


@needs_pwsh
def test_without_the_installer_wait_the_two_overlap(tmp_path):
    """The fake reproduces what the guest did: msiexec returning is not enough."""
    _, events, _ = _run_script(tmp_path, wait_for_installer=False)

    at = _order(events)
    assert at['invoked qemu-ga-x86_64.msi'] < at['end virtio-win-gt-x64.msi'], events


@needs_pwsh
def test_a_clean_run_removes_the_staging_folder_and_keeps_its_log(tmp_path):
    _, _, staging = _run_script(tmp_path)

    log = (tmp_path / 'firstboot.log').read_text()
    assert not staging.exists()
    assert 'first boot finished' in log
    assert 'failed' not in log


@needs_pwsh
def test_a_failed_install_is_retried(tmp_path):
    # 1603 is what msiexec returns, but a Unix exit status has eight bits.
    _, events, staging = _run_script(tmp_path, exit_codes={'qemu-ga-x86_64.msi': 99})

    log = (tmp_path / 'firstboot.log').read_text()
    assert events.count('invoked qemu-ga-x86_64.msi') == 2
    assert 'qemu-ga-x86_64.msi attempt 1 exit 99' in log
    assert 'qemu-ga-x86_64.msi attempt 2 exit 0' in log
    assert not staging.exists()


@needs_pwsh
def test_an_install_that_keeps_failing_leaves_a_marker_and_its_logs(tmp_path):
    _, events, staging = _run_script(tmp_path, exit_codes={'qemu-ga-x86_64.msi': 99},
                                     fail_always=True)

    log = (tmp_path / 'firstboot.log').read_text()
    assert events.count('invoked qemu-ga-x86_64.msi') == 3
    assert (staging / 'install-failed').exists()
    assert 'first boot finished with a failed install' in log


@needs_pwsh
def test_the_boot_drivers_are_armed_and_the_service_deletes_itself(tmp_path):
    _run_script(tmp_path)

    sc = (tmp_path / 'sc.log').read_text()
    assert 'config vioscsi start= boot' in sc
    assert 'config viostor start= boot' in sc
    assert f'delete {fb.FIRST_BOOT_SERVICE}' in sc


def test_the_service_only_launches_the_script_and_returns():
    """While the service is start-pending the SCM holds its database lock, and the agent's
    COM registration failed on exactly that (0x8007041F). `start` without /wait returns."""
    command = fb.FIRST_BOOT_SERVICE_COMMAND
    assert command.startswith('cmd.exe /c start "" powershell.exe ')
    assert '/wait' not in command
    assert 'msiexec' not in command
    assert command.endswith('-File "C:\\qemu\\firstboot.ps1"')


def test_the_staged_script_is_the_script_with_windows_line_ends(tmp_path):
    line = fb.first_boot_script_staging('PEGADIR')
    done = subprocess.run(['bash', '-c', line], capture_output=True, text=True,
                          env={'PATH': '/usr/bin:/bin', 'PEGADIR': str(tmp_path)})

    written = (tmp_path / 'firstboot.ps1').read_bytes()
    assert 'FIRSTBOOT_STAGED firstboot.ps1' in done.stdout
    assert written == fb.FIRST_BOOT_SCRIPT.replace('\n', '\r\n').encode('utf-8')
    assert base64.b64encode(written).decode() in line
