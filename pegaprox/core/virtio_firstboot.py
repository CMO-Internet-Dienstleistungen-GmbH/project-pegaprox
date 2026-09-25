"""The first-boot install that follows an offline VirtIO injection.

The injection stages the driver MSI and the guest agent MSI in C:\\qemu and registers
a one-shot service, PegaProxFirstBoot, to install them at the guest's first boot. What
runs is `firstboot.ps1`, written next to the installers; the service only launches it.
Its own log is C:\\Windows\\Temp\\pegaprox-firstboot.log, which stays after a clean run.

Why a script file and not the `cmd /c` chain the service carried before:

- The chain ran inside the service's own start. cmd.exe never reports SERVICE_RUNNING,
  so the service stays start-pending until the SCM gives up on it, and the SCM holds its
  database lock while a service is starting. The guest agent's MSI starts services
  during its COM registration, and failed with 0x8007041F (ERROR_SERVICE_DATABASE_LOCKED)
  and 1708. The launcher below returns at once, so no service is starting while the
  installers run.
- Starting the installers one after the other did not keep them apart on the guest, not
  even with `start "" /wait` in front of each: both logs start two to three seconds after
  each other and overlap, and which of the two fails varies. The script therefore does
  not rely on a process exiting. Windows Installer holds the
  `Global\\_MSIExecute` mutex while an installation executes, and the script waits for
  that mutex to be free before each installer and again after it.

The script is PowerShell 2.0 syntax on purpose, the version Server 2008 R2 ships.
"""
import base64

FIRST_BOOT_DIR = 'C:\\qemu'
FIRST_BOOT_SCRIPT_NAME = 'firstboot.ps1'
FIRST_BOOT_SERVICE = 'PegaProxFirstBoot'

# `start` without /wait hands the script to a process of its own and returns, so the
# service process exits within a second. The SCM logs that the service stopped; the
# service has ErrorControl 0, so nothing else follows from it.
FIRST_BOOT_SERVICE_COMMAND = (
    'cmd.exe /c start "" powershell.exe -NoProfile -NonInteractive '
    '-ExecutionPolicy Bypass -File "' + FIRST_BOOT_DIR + '\\' + FIRST_BOOT_SCRIPT_NAME + '"'
)

FIRST_BOOT_SCRIPT = r'''# PegaProx first-boot install. Written by the VirtIO injection, run once as LocalSystem.
$dir = 'C:\qemu'
# Outside the staging folder, so it survives the clean-up of a successful run.
$log = Join-Path $env:SystemRoot 'Temp\pegaprox-firstboot.log'
$failed = Join-Path $dir 'install-failed'
$service = 'PegaProxFirstBoot'
$attempts = 3
$idleTimeoutSeconds = 1800
$pollSeconds = 2

function Write-Step([string]$text) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -Path $log -Value ($stamp + ' ' + $text)
}

# Windows Installer holds this mutex while an installation executes. Any failure to open
# it other than "it does not exist" means it does.
function Test-InstallerBusy {
    try {
        $mutex = [System.Threading.Mutex]::OpenExisting('Global\_MSIExecute')
        $mutex.Close()
        return $true
    } catch [System.Threading.WaitHandleCannotBeOpenedException] {
        return $false
    } catch {
        return $true
    }
}

function Wait-InstallerIdle([string]$reason) {
    $deadline = (Get-Date).AddSeconds($idleTimeoutSeconds)
    $waited = $false
    while (Test-InstallerBusy) {
        if ((Get-Date) -gt $deadline) {
            Write-Step ('Windows Installer still busy after ' + $idleTimeoutSeconds + ' s (' + $reason + ')')
            return $false
        }
        $waited = $true
        Start-Sleep -Seconds $pollSeconds
    }
    if ($waited) { Write-Step ('Windows Installer idle (' + $reason + ')') }
    return $true
}

# The launcher has to be gone before anything starts a service: while PegaProxFirstBoot
# is start-pending the SCM holds its database lock.
function Wait-LauncherGone {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        $svc = Get-Service -Name $service -ErrorAction SilentlyContinue
        if ($svc -eq $null -or $svc.Status -ne 'StartPending') { return }
        Start-Sleep -Seconds 1
    }
    Write-Step ($service + ' still start-pending after 120 s')
}

# 0 is installed, 3010 installed with a restart pending; everything else failed.
function Install-Msi([string]$name, [string]$logName, [string[]]$properties) {
    $msi = Join-Path $dir $name
    if (-not (Test-Path $msi)) {
        Write-Step ($name + ' not staged, skipped')
        return $true
    }
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        if (-not (Wait-InstallerIdle ('before ' + $name))) { return $false }
        $arguments = @('/i', ('"' + $msi + '"')) + $properties +
            @('/quiet', '/norestart', '/l*v+', ('"' + (Join-Path $dir $logName) + '"'))
        Write-Step ($name + ' attempt ' + $attempt + ' started')
        $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments -Wait -PassThru
        $code = $process.ExitCode
        # msiexec returning is not taken as the installation having finished.
        $idle = Wait-InstallerIdle ('after ' + $name)
        Write-Step ($name + ' attempt ' + $attempt + ' exit ' + $code)
        if ($idle -and ($code -eq 0 -or $code -eq 3010)) { return $true }
    }
    return $false
}

Write-Step 'first boot started'
Wait-LauncherGone
$ok = $true
# The drivers first: the agent talks over the VirtIO serial port they install. The
# injection stages whichever of the two driver MSIs the ISO carries, under its own name,
# and Install-Msi skips the one that is not there.
foreach ($driverMsi in @('virtio-win-gt-x64.msi', 'virtio-win-gt-x86.msi')) {
    if (-not (Install-Msi $driverMsi 'msi.log' @('ADDLOCAL=ALL'))) { $ok = $false }
}
if (-not (Install-Msi 'qemu-ga-x86_64.msi' 'qemu-ga.log' @())) { $ok = $false }

# The MSI registers vioscsi and viostor as demand-start. Boot-start lets the controller
# be switched to VirtIO SCSI later without an INACCESSIBLE_BOOT_DEVICE.
& sc.exe config vioscsi start= boot 2>&1 | Out-File -Append (Join-Path $dir 'bootarm.log')
& sc.exe config viostor start= boot 2>&1 | Out-File -Append (Join-Path $dir 'bootarm.log')
& sc.exe delete $service 2>&1 | Out-File -Append (Join-Path $dir 'service.log')
Remove-Item -Path (Join-Path $dir '*.msi') -Force -ErrorAction SilentlyContinue

# Nothing of the import stays on a guest that installed cleanly; one that did not keeps
# its logs.
if ($ok) {
    Write-Step 'first boot finished'
    Set-Location -Path 'C:\'
    Remove-Item -Path $dir -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Set-Content -Path $failed -Value ''
    Write-Step 'first boot finished with a failed install'
}
'''


def first_boot_script_staging(target_dir_var):
    """The shell line that writes firstboot.ps1 into the staging folder on the node.

    The script travels base64-encoded, so no character of it has to survive the heredocs
    it is nested in. CRLF, because it is a Windows file.
    """
    body = FIRST_BOOT_SCRIPT.replace('\r\n', '\n').replace('\n', '\r\n')
    encoded = base64.b64encode(body.encode('utf-8')).decode('ascii')
    return (
        f"printf '%s' '{encoded}' | base64 -d > \"${target_dir_var}/{FIRST_BOOT_SCRIPT_NAME}\" "
        f"&& echo 'FIRSTBOOT_STAGED {FIRST_BOOT_SCRIPT_NAME}' "
        f"|| echo 'FIRSTBOOT_FAILED {FIRST_BOOT_SCRIPT_NAME}'\n"
    )
