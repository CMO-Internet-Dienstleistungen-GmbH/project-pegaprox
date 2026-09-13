"""The PowerShell this product sends to a Hyper-V host.

Kept in one file, as data, for three reasons. It can be read as a whole, so what the
product asks a customer's host is reviewable without tracing calls. It can be executed
against a real PowerShell parser in tests. And the read-only ones can be checked as a
group for a mutating verb, which is what `hyperv_client.assert_read_only` does.

## What every script has to obey

**Windows PowerShell 5.1.** Windows Server runs it by default and it is not PowerShell 7.
`ConvertTo-Json` there takes only `-InputObject`, `-Depth` and `-Compress`; `-AsArray`
arrived in PowerShell 6 and fails the whole call. Nothing here may use a parameter or
operator newer than 5.1.

**Arrays, always.** `ConvertTo-Json -InputObject @($items)` is the only form that yields
`[]` for nothing, `[{…}]` for one and a full array for many. Piping into `ConvertTo-Json`
does not: the pipeline unrolls the array and a single result comes back as a bare object,
which the Python side would then have to guess about.

**Explicit `-Depth`.** The 5.1 default of 2 truncates nested values without saying so, and
a half-read object looks exactly like a complete one.

**No interpolation of caller data.** A VM is addressed by GUID through a parameter, never
by pasting a name into the script text. A VM named `x'; Remove-VM -Name *` is a legal
Hyper-V name.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------

#: Versions and capabilities of the host itself. Deliberately does not read
#: $env:COMPUTERNAME: this repository is public, and the host's own name is the one value
#: the product could learn that it could not strip from its own output afterwards.
HOST_FACTS = r'''
$os = Get-CimInstance Win32_OperatingSystem
$hv = Get-Module -ListAvailable Hyper-V | Select-Object -First 1
ConvertTo-Json -Depth 4 -Compress -InputObject ([pscustomobject]@{
    OSCaption      = $os.Caption
    OSVersion      = $os.Version
    PSVersion      = $PSVersionTable.PSVersion.ToString()
    HyperVModule   = if ($hv) { $hv.Version.ToString() } else { $null }
    VMHostVersion  = (Get-VMHost).ErrorState
})
'''

#: Every VM on the host, with enough to list and identify them. The per-VM detail that
#: costs a call each (disks, NICs, firmware) is deliberately not here: an inventory of two
#: hundred VMs would take minutes.
VM_INVENTORY = r'''
# Where-Object drops a null before it reaches the body. A host with no VMs, or a
# cmdlet that answers with nothing, would otherwise produce one object made entirely
# of null properties — an inventory entry for a VM that does not exist.
$items = @(Get-VM) | Where-Object { $_ } | ForEach-Object {
    [pscustomobject]@{
        Id                   = $_.Id.ToString()
        Name                 = $_.Name
        State                = $_.State.ToString()
        Generation           = $_.Generation
        ProcessorCount       = $_.ProcessorCount
        MemoryStartup        = $_.MemoryStartup
        MemoryMinimum        = $_.MemoryMinimum
        MemoryMaximum        = $_.MemoryMaximum
        DynamicMemoryEnabled = $_.DynamicMemoryEnabled
        Version              = $_.Version
        Uptime               = $_.Uptime.TotalSeconds
    }
}
ConvertTo-Json -Depth 4 -Compress -InputObject @($items)
'''

#: Everything about one VM that a migration has to reproduce or refuse. One call rather
#: than six, because each round trip is a WSMan exchange and the detail view is opened per
#: VM by a person waiting for it.
#:
#: Takes the VM's GUID as a parameter. Firmware differs by generation: generation 2 has
#: Get-VMFirmware and Secure Boot, generation 1 has Get-VMBios and neither.
VM_DETAIL = r'''
param([Parameter(Mandatory=$true)][string]$VmId)

$vm = Get-VM -Id $VmId -ErrorAction Stop

$disks = @(@(Get-VMHardDiskDrive -VM $vm) | Where-Object { $_ } | ForEach-Object {
    $drive = $_
    $vhd = $null
    try { $vhd = Get-VHD -Path $drive.Path -ErrorAction Stop } catch { }
    [pscustomobject]@{
        Path               = $drive.Path
        ControllerType     = $drive.ControllerType.ToString()
        ControllerNumber   = $drive.ControllerNumber
        ControllerLocation = $drive.ControllerLocation
        VhdFormat          = if ($vhd) { $vhd.VhdFormat.ToString() } else { $null }
        VhdType            = if ($vhd) { $vhd.VhdType.ToString() } else { $null }
        ParentPath         = if ($vhd) { $vhd.ParentPath } else { $null }
        FileSize           = if ($vhd) { $vhd.FileSize } else { $null }
        Size               = if ($vhd) { $vhd.Size } else { $null }
        Attached           = if ($vhd) { $vhd.Attached } else { $null }
        DiskIdentifier     = if ($vhd) { $vhd.DiskIdentifier } else { $null }
        VhdReadError       = if ($vhd) { $null } else { 'Get-VHD could not read this disk' }
    }
})

$nics = @(@(Get-VMNetworkAdapter -VM $vm) | Where-Object { $_ } | ForEach-Object {
    $nic = $_
    $vlan = $null
    try { $vlan = Get-VMNetworkAdapterVlan -VMNetworkAdapter $nic -ErrorAction Stop } catch { }
    [pscustomobject]@{
        Name                     = $nic.Name
        MacAddress               = $nic.MacAddress
        DynamicMacAddressEnabled = $nic.DynamicMacAddressEnabled
        SwitchName               = $nic.SwitchName
        Connected                = $nic.Connected
        VlanMode                 = if ($vlan) { $vlan.OperationMode.ToString() } else { $null }
        VlanId                   = if ($vlan) { $vlan.AccessVlanId } else { $null }
    }
})

$dvds = @(@(Get-VMDvdDrive -VM $vm) | Where-Object { $_ } | ForEach-Object {
    [pscustomobject]@{
        Path               = $_.Path
        ControllerNumber   = $_.ControllerNumber
        ControllerLocation = $_.ControllerLocation
    }
})

$checkpoints = @(Get-VMSnapshot -VM $vm -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{
        Name               = $_.Name
        SnapshotType       = $_.SnapshotType.ToString()
        CreationTime       = $_.CreationTime.ToString('o')
        ParentSnapshotName = $_.ParentSnapshotName
    }
})

$firmware = $null
if ($vm.Generation -eq 2) {
    $fw = Get-VMFirmware -VM $vm
    $firmware = [pscustomobject]@{
        SecureBoot         = $fw.SecureBoot.ToString()
        SecureBootTemplate = $fw.SecureBootTemplate
        BootOrder          = @($fw.BootOrder | ForEach-Object { $_.BootType.ToString() })
    }
} else {
    $bios = Get-VMBios -VM $vm
    $firmware = [pscustomobject]@{
        SecureBoot         = 'Off'
        SecureBootTemplate = $null
        BootOrder          = @($bios.StartupOrder | ForEach-Object { $_.ToString() })
    }
}

$tpmEnabled = $false
try { $tpmEnabled = (Get-VMSecurity -VM $vm -ErrorAction Stop).TpmEnabled } catch { }

ConvertTo-Json -Depth 6 -Compress -InputObject ([pscustomobject]@{
    Id                   = $vm.Id.ToString()
    Name                 = $vm.Name
    State                = $vm.State.ToString()
    Generation           = $vm.Generation
    ProcessorCount       = $vm.ProcessorCount
    MemoryStartup        = $vm.MemoryStartup
    MemoryMinimum        = $vm.MemoryMinimum
    MemoryMaximum        = $vm.MemoryMaximum
    DynamicMemoryEnabled = $vm.DynamicMemoryEnabled
    Version              = $vm.Version
    Notes                = ''
    Disks                = $disks
    NetworkAdapters      = $nics
    DvdDrives            = $dvds
    Checkpoints          = $checkpoints
    Firmware             = $firmware
    TpmEnabled           = $tpmEnabled
})
'''

#: The disks of one VM and their parent chains, re-read immediately before a transfer.
#: Separate from VM_DETAIL because it is asked again at the last moment: the point is to
#: catch a checkpoint taken, or a merge started, between preflight and the copy.
VM_DISK_CHAIN = r'''
param([Parameter(Mandatory=$true)][string]$VmId)

$vm = Get-VM -Id $VmId -ErrorAction Stop
$items = @(@(Get-VMHardDiskDrive -VM $vm) | Where-Object { $_ } | ForEach-Object {
    $path = $_.Path
    $chain = @()
    $current = $path
    # Walk to the root of the chain. A VM ready to migrate has a chain of one; anything
    # longer means checkpoint data that has not been merged.
    while ($current) {
        $vhd = $null
        try { $vhd = Get-VHD -Path $current -ErrorAction Stop } catch { break }
        $chain += [pscustomobject]@{
            Path     = $vhd.Path
            VhdType  = $vhd.VhdType.ToString()
            Size     = $vhd.Size
            FileSize = $vhd.FileSize
        }
        $current = $vhd.ParentPath
    }
    [pscustomobject]@{
        Path        = $path
        ChainLength = $chain.Count
        Chain       = $chain
    }
})
ConvertTo-Json -Depth 6 -Compress -InputObject @($items)
'''

#: Just the state and checkpoint count of one VM. The cheapest possible question, asked
#: right before disks are read, so a VM started by somebody else in the meantime is caught.
VM_STATE = r'''
param([Parameter(Mandatory=$true)][string]$VmId)

$vm = Get-VM -Id $VmId -ErrorAction Stop
ConvertTo-Json -Depth 3 -Compress -InputObject ([pscustomobject]@{
    Id              = $vm.Id.ToString()
    State           = $vm.State.ToString()
    CheckpointCount = @(Get-VMSnapshot -VM $vm -ErrorAction SilentlyContinue).Count
})
'''

#: Does this host's Hyper-V module actually expose the properties this product reads?
#:
#: Microsoft documents the Hyper-V cmdlets' *parameters* thoroughly and their returned
#: objects' *properties* almost not at all. Get-VHD is the one exception. Every other name
#: below is convention rather than contract, which means an unnoticed rename would not
#: raise anything: the property would simply be absent and every VM would report null.
#:
#: So the names are checked against the live objects once, at connection time, and what is
#: missing is reported. A wrong answer about a VM's generation or checkpoint count decides
#: whether a migration is allowed to touch its disks, and "the value was null" is not
#: something to discover afterwards.
VERIFY_PROPERTIES = r"""
$expected = @{
    'VM'        = @('Id','Name','State','Generation','ProcessorCount','MemoryStartup',
                    'MemoryMinimum','MemoryMaximum','DynamicMemoryEnabled','Version','Uptime')
    'HardDisk'  = @('Path','ControllerType','ControllerNumber','ControllerLocation')
    'VHD'       = @('Path','VhdFormat','VhdType','ParentPath','FileSize','Size','Attached')
    'NetAdapter'= @('Name','MacAddress','DynamicMacAddressEnabled','SwitchName','Connected')
    'Snapshot'  = @('Name','SnapshotType','CreationTime')
    'Firmware'  = @('SecureBoot','SecureBootTemplate','BootOrder')
    'Security'  = @('TpmEnabled')
}

function Get-MissingMembers {
    param($Object, [string[]]$Names)
    if (-not $Object) { return @('<no object to inspect>') }
    $present = @($Object | Get-Member -MemberType Properties | ForEach-Object { $_.Name })
    return @($Names | Where-Object { $present -notcontains $_ })
}

$vm = @(Get-VM)[0]
$result = @{}

$result['VM'] = Get-MissingMembers -Object $vm -Names $expected['VM']

if ($vm) {
    $disk = @(Get-VMHardDiskDrive -VM $vm)[0]
    $result['HardDisk'] = Get-MissingMembers -Object $disk -Names $expected['HardDisk']
    if ($disk) {
        $vhd = $null
        try { $vhd = Get-VHD -Path $disk.Path -ErrorAction Stop } catch { }
        $result['VHD'] = Get-MissingMembers -Object $vhd -Names $expected['VHD']
    }
    $result['NetAdapter'] = Get-MissingMembers -Object (@(Get-VMNetworkAdapter -VM $vm)[0]) `
                                               -Names $expected['NetAdapter']
    $result['Snapshot'] = Get-MissingMembers -Object (@(Get-VMSnapshot -VM $vm -ErrorAction SilentlyContinue)[0]) `
                                             -Names $expected['Snapshot']
    if ($vm.Generation -eq 2) {
        $result['Firmware'] = Get-MissingMembers -Object (Get-VMFirmware -VM $vm) -Names $expected['Firmware']
    }
    $sec = $null
    try { $sec = Get-VMSecurity -VM $vm -ErrorAction Stop } catch { }
    $result['Security'] = Get-MissingMembers -Object $sec -Names $expected['Security']
}

ConvertTo-Json -Depth 5 -Compress -InputObject ([pscustomobject]@{
    InspectedVM = if ($vm) { $vm.Name } else { $null }
    Missing     = [pscustomobject]$result
})
"""

#: Whether Hyper-V is still merging a deleted checkpoint's differencing file into its
#: parent, read from WMI because that is the only place it is documented.
#:
#: This is the difference between a migration that works and one that quietly copies a file
#: being written to. Remove-VMSnapshot returns as soon as the checkpoint entry is gone; the
#: merge continues in the background, and neither the cmdlet's return nor the checkpoint
#: list says anything about it. Msvm_ComputerSystem.OperationalStatus[1] does:
#: 32772 is "Merging Disks", and 32770 is "Deleting Snapshot".
#:
#: The states are numbers rather than strings on purpose. The status *descriptions* are not
#: enumerated in the documentation, so matching on their text would be matching on something
#: nobody promised.
VM_MERGE_STATE = r"""
param([Parameter(Mandatory=$true)][string]$VmId)

# Msvm_ComputerSystem.Name carries the VM's GUID. Where-Object rather than -Filter so the
# id is compared as a value and never becomes part of a query string.
$cs = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_ComputerSystem |
      Where-Object { $_.Name -eq $VmId }

if (-not $cs) { throw "No Msvm_ComputerSystem for VM $VmId." }

$status = @($cs.OperationalStatus)
$primary = if ($status.Count -gt 0) { [int]$status[0] } else { $null }
$secondary = if ($status.Count -gt 1) { [int]$status[1] } else { $null }

ConvertTo-Json -Depth 4 -Compress -InputObject ([pscustomobject]@{
    PrimaryStatus   = $primary
    SecondaryStatus = $secondary
    EnabledState    = [int]$cs.EnabledState
    HealthState     = [int]$cs.HealthState
})
"""

#: ISO files the host can offer. Read from the library paths the operator configured,
#: which are passed in rather than discovered, so the product never walks a customer's
#: whole filesystem.
ISO_LIBRARY = r'''
param([Parameter(Mandatory=$true)][string[]]$Paths)

$items = @($Paths | ForEach-Object {
    $root = $_
    if (Test-Path -LiteralPath $root) {
        Get-ChildItem -LiteralPath $root -Filter *.iso -File -ErrorAction SilentlyContinue |
            ForEach-Object {
                [pscustomobject]@{
                    Path         = $_.FullName
                    Name         = $_.Name
                    Length       = $_.Length
                    LastModified = $_.LastWriteTimeUtc.ToString('o')
                }
            }
    }
})
ConvertTo-Json -Depth 4 -Compress -InputObject @($items)
'''

#: Every read-only script, so tests can assert the whole set at once rather than one at a
#: time as somebody remembers to add them.
READ_ONLY_SCRIPTS = {
    'HOST_FACTS': HOST_FACTS,
    'VM_INVENTORY': VM_INVENTORY,
    'VM_DETAIL': VM_DETAIL,
    'VM_DISK_CHAIN': VM_DISK_CHAIN,
    'VM_STATE': VM_STATE,
    'VM_MERGE_STATE': VM_MERGE_STATE,
    'ISO_LIBRARY': ISO_LIBRARY,
    'VERIFY_PROPERTIES': VERIFY_PROPERTIES,
}


# --------------------------------------------------------------------------------------
# Acting
#
# Each of these changes the host and is reachable only through a named method that audits
# it. They never force: a migration that hard-powers a VM to get on with it has thrown
# away the orderly shutdown that makes the copy trustworthy.
# --------------------------------------------------------------------------------------

#: Start a VM. Used for preparation — installing drivers, for instance — never as part of
#: a migration run.
START_VM = r'''
param([Parameter(Mandatory=$true)][string]$VmId)
$vm = Get-VM -Id $VmId -ErrorAction Stop
Start-VM -VM $vm -ErrorAction Stop | Out-Null
ConvertTo-Json -Depth 2 -Compress -InputObject ([pscustomobject]@{
    Id = $vm.Id.ToString(); State = (Get-VM -Id $VmId).State.ToString()
})
'''

#: Ask the guest to shut down, and report honestly whether it did.
#:
#: No -Force anywhere. Stop-VM without it requests an orderly shutdown through the
#: integration services; with it, the VM is pulled off the power. A guest that ignores the
#: request is a fact the operator has to see, not a reason to take the disks away from it.
SHUTDOWN_VM = r'''
param(
    [Parameter(Mandatory=$true)][string]$VmId,
    [Parameter(Mandatory=$true)][int]$TimeoutSeconds
)

$vm = Get-VM -Id $VmId -ErrorAction Stop
if ($vm.State -eq 'Off') {
    ConvertTo-Json -Depth 2 -Compress -InputObject ([pscustomobject]@{
        Id = $vm.Id.ToString(); State = 'Off'; ShutdownRequested = $false
        Reason = 'The VM was already off.'
    })
    return
}

Stop-VM -VM $vm -ErrorAction Stop | Out-Null

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if ((Get-VM -Id $VmId).State -eq 'Off') { break }
    Start-Sleep -Seconds 2
}

$final = (Get-VM -Id $VmId).State.ToString()
ConvertTo-Json -Depth 2 -Compress -InputObject ([pscustomobject]@{
    Id = $VmId
    State = $final
    ShutdownRequested = $true
    Reason = if ($final -eq 'Off') { $null }
             else { 'The guest did not shut down within the timeout. It was not forced off.' }
})
'''

#: Delete one checkpoint, or all of them, and report what the disks look like afterwards.
#:
#: The returned chain is the point. Remove-VMSnapshot returns as soon as the checkpoint
#: entry is gone, while the merge of its differencing file into the parent continues in the
#: background. Treating the entry's disappearance as completion is how a migration reads a
#: disk that is still being written to.
REMOVE_CHECKPOINTS = r'''
param(
    [Parameter(Mandatory=$true)][string]$VmId,
    [string]$CheckpointName,
    [bool]$All = $false
)

$vm = Get-VM -Id $VmId -ErrorAction Stop
$snapshots = @(Get-VMSnapshot -VM $vm -ErrorAction SilentlyContinue)
if (-not $All) {
    $snapshots = @($snapshots | Where-Object { $_.Name -eq $CheckpointName })
    if ($snapshots.Count -eq 0) { throw "No checkpoint named '$CheckpointName' on this VM." }
}

foreach ($snapshot in $snapshots) {
    Remove-VMSnapshot -VMSnapshot $snapshot -ErrorAction Stop
}

# What is left, read from the disks rather than from the checkpoint list. The caller
# decides whether the merge has finished; this only reports.
$disks = @(Get-VMHardDiskDrive -VM $vm | ForEach-Object {
    $vhd = $null
    try { $vhd = Get-VHD -Path $_.Path -ErrorAction Stop } catch { }
    [pscustomobject]@{
        Path       = $_.Path
        VhdType    = if ($vhd) { $vhd.VhdType.ToString() } else { $null }
        ParentPath = if ($vhd) { $vhd.ParentPath } else { $null }
    }
})

ConvertTo-Json -Depth 5 -Compress -InputObject ([pscustomobject]@{
    RemovedCount        = $snapshots.Count
    RemainingCheckpoints = @(Get-VMSnapshot -VM $vm -ErrorAction SilentlyContinue).Count
    Disks               = $disks
})
'''

#: Attach an ISO, adding a DVD drive if the VM has none.
#:
#: The path is passed as a parameter and used with -LiteralPath, so a file name containing
#: a quote or a bracket is a file name and not script.
MOUNT_ISO = r'''
param(
    [Parameter(Mandatory=$true)][string]$VmId,
    [Parameter(Mandatory=$true)][string]$IsoPath
)

$vm = Get-VM -Id $VmId -ErrorAction Stop
if (-not (Test-Path -LiteralPath $IsoPath)) { throw "The host cannot reach '$IsoPath'." }

$drive = @(Get-VMDvdDrive -VM $vm) | Select-Object -First 1
if (-not $drive) {
    Add-VMDvdDrive -VM $vm -ErrorAction Stop
    $drive = @(Get-VMDvdDrive -VM $vm) | Select-Object -First 1
}
Set-VMDvdDrive -VMDvdDrive $drive -Path $IsoPath -ErrorAction Stop

ConvertTo-Json -Depth 3 -Compress -InputObject ([pscustomobject]@{
    Path = (@(Get-VMDvdDrive -VM $vm) | Select-Object -First 1).Path
})
'''

#: Detach whatever is in the DVD drive, leaving the drive itself in place.
EJECT_ISO = r'''
param([Parameter(Mandatory=$true)][string]$VmId)

$vm = Get-VM -Id $VmId -ErrorAction Stop
$drive = @(Get-VMDvdDrive -VM $vm) | Select-Object -First 1
if ($drive) { Set-VMDvdDrive -VMDvdDrive $drive -Path $null -ErrorAction Stop }

ConvertTo-Json -Depth 3 -Compress -InputObject ([pscustomobject]@{
    Path = if ($drive) { (@(Get-VMDvdDrive -VM $vm) | Select-Object -First 1).Path } else { $null }
})
'''

#: Every mutating script, so tests can hold that none of them forces anything.
ACTION_SCRIPTS = {
    'START_VM': START_VM,
    'SHUTDOWN_VM': SHUTDOWN_VM,
    'REMOVE_CHECKPOINTS': REMOVE_CHECKPOINTS,
    'MOUNT_ISO': MOUNT_ISO,
    'EJECT_ISO': EJECT_ISO,
}

ALL_SCRIPTS = {**READ_ONLY_SCRIPTS, **ACTION_SCRIPTS}
