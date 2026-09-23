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
        # What the guest is using right now, as opposed to what it may use. The VM lists
        # render both, and without these the shared resource table printed "NaN MB".
        MemoryAssigned       = $_.MemoryAssigned
        CPUUsage             = $_.CPUUsage
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
    #: Whether this guest can be asked to shut itself down, which decides whether a
    #: migration can prepare it at all. Selected by .NET type, not by name: on a German
    #: host the service is called 'Herunterfahren', and a name comparison would report
    #: every guest there as unable to shut down.
    IntegrationServices  = @(Get-VMIntegrationService -VM $vm -ErrorAction SilentlyContinue |
        ForEach-Object {
            [pscustomobject]@{
                Kind    = $_.GetType().Name
                Enabled = [bool]$_.Enabled
                #: The numeric CIM status, because PrimaryStatusDescription is localised -
                #: a German host answers 'Kein Kontakt' where an English one answers 'No
                #: Contact', and a comparison against either is a comparison that fails
                #: somewhere. 2 is OK; everything else means the guest is not answering.
                OperationalStatus = [int]($_.PrimaryOperationalStatus)
                #: Kept for the operator, never compared against.
                Status  = [string]$_.PrimaryStatusDescription
            }
        })
    #: What brings this VM back up on its own. On a standalone host that is
    #: AutomaticStartAction, which fires when the host boots and only for VMs that were
    #: running when it went down. A migration has to clear it before shutting the source
    #: down, or a host restart mid-transfer starts the original beside its copy.
    AutomaticStartAction = $vm.AutomaticStartAction.ToString()
    AutomaticStartDelay  = $vm.AutomaticStartDelay
    AutomaticStopAction  = $vm.AutomaticStopAction.ToString()
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

#: What the disks of one VM say about the guest on them, without starting it.
#:
#: `Get-WindowsImage` reads a VHDX in place — no mount, no attach, nothing written. It is
#: the only way to know a stopped guest's Windows version: the integration services report
#: that over KVP, and those items exist only while the VM runs (measured 2026-09-16: full
#: while running, empty three seconds after the guest finished shutting down). A migration
#: requires a stopped VM, so at the moment the wizard needs the answer, the host no longer
#: has it — but the disk still does.
#:
#: Costs about two to three seconds per disk on a 100 GiB VHDX. Asked once, when the plan
#: is built, rather than on every preflight refresh.
#:
#: Errors are per disk and never raise: a data disk with no Windows on it is an ordinary
#: answer, not a failure, and must not be read as "this guest is not Windows" either —
#: only the disk it was asked about.
VM_IMAGE_FACTS = r"""
param([Parameter(Mandatory=$true)][string]$VmId)

$vm = Get-VM -Id $VmId -ErrorAction Stop
$items = @(@(Get-VMHardDiskDrive -VM $vm) | Where-Object { $_ } | ForEach-Object {
    $path = $_.Path
    $row = [ordered]@{
        Path            = $path
        ControllerType  = "$($_.ControllerType)"
        ControllerNumber = $_.ControllerNumber
        ControllerLocation = $_.ControllerLocation
        Exists          = $false
        Attached        = $null
        VhdType         = ''
        ParentPath      = ''
        Size            = 0
        FileSize        = 0
        Windows         = $false
        WindowsError    = ''
        Build           = 0
        Version         = ''
        SPBuild         = ''
        Architecture    = $null
        EditionId       = ''
        InstallationType = ''
        SystemRoot      = ''
        Seconds         = 0
    }
    if (Test-Path -LiteralPath $path) {
        $row.Exists = $true
        try {
            $vhd = Get-VHD -Path $path -ErrorAction Stop
            $row.Attached   = [bool]$vhd.Attached
            $row.VhdType    = "$($vhd.VhdType)"
            $row.ParentPath = "$($vhd.ParentPath)"
            $row.Size       = [int64]$vhd.Size
            $row.FileSize   = [int64]$vhd.FileSize
        } catch { }

        $sw = [Diagnostics.Stopwatch]::StartNew()
        try {
            # Index 1 is the installed system on a virtual disk; a VHDX carries no second
            # image the way an install WIM does.
            $img = Get-WindowsImage -ImagePath $path -Index 1 -ErrorAction Stop
            $row.Windows          = $true
            $row.Build            = [int]$img.Build
            $row.Version          = "$($img.Version)"
            $row.SPBuild          = "$($img.SPBuild)"
            $row.Architecture     = $img.Architecture
            # Kept for what their absence says, not for their contents: these two come
            # out of the guest's SOFTWARE hive and can be empty while the version fields
            # are filled. That is the same hive the driver injection writes into later,
            # and it fails there with "Operation not supported" — after the copy.
            $row.EditionId        = "$($img.EditionId)"
            $row.InstallationType = "$($img.InstallationType)"
            $row.SystemRoot       = "$($img.SystemRoot)"
        } catch {
            $row.WindowsError = $_.Exception.Message.Split([Environment]::NewLine)[0]
        }
        $sw.Stop()
        $row.Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
    [pscustomobject]$row
})
ConvertTo-Json -Depth 4 -Compress -InputObject @($items)
"""

#: What the inside of a stopped VM's disks says about whether it can be migrated.
#:
#: Everything that makes a migration fail late lives in the file system and is invisible
#: from outside the disk: a guest that hibernated, whose saved session cannot survive a
#: change of chipset and controller; a file system nobody dismounted cleanly; a volume
#: that carries no Windows where one was expected.
#:
#: Mounting is the only way in, so the shape of this script is decided by one thing: the
#: host must give the disk back. A VHDX left attached is a VM that cannot start. Hence
#: `-ReadOnly`, hence the `finally`, and hence the `Attached` reading afterwards that says
#: whether the release worked.
#:
#: No drive letters are assigned — the volumes are reached through their own GUID paths
#: (`\\?\Volume{...}`), which reads the same files and changes nothing on the host.
#: Measured 2026-09-16: six seconds for a 100 GiB disk with three volumes.
VM_DISK_INSPECTION = r"""
param([Parameter(Mandatory=$true)][string]$VmId)

$ErrorActionPreference = 'Continue'
$vm = Get-VM -Id $VmId -ErrorAction Stop
$fsutil = Join-Path $env:SystemRoot 'System32\fsutil.exe'
$result = [ordered]@{
    State = "$($vm.State)"; Inspected = $false; Error = ''; Disks = @()
}
if ($vm.State -ne 'Off') {
    # A running VM writes to these disks. Mounting them on the host as well is not a
    # measurement, it is a second writer.
    $result.Error = "The VM is $($vm.State); its disks are only inspected while it is off."
    [pscustomobject]$result | ConvertTo-Json -Depth 6 -Compress
    return
}

foreach ($d in @(Get-VMHardDiskDrive -VM $vm)) {
    $entry = [ordered]@{
        Path = $d.Path; Mounted = $false; Error = ''; AttachedAfter = $null; Volumes = @()
        Partitions = @()
    }
    try {
        if ((Get-VHD -Path $d.Path -ErrorAction Stop).Attached) {
            $entry.Error = 'The disk is already attached to something; not inspected.'
            $result.Disks += [pscustomobject]$entry
            continue
        }
    } catch {
        $entry.Error = $_.Exception.Message.Split([Environment]::NewLine)[0]
        $result.Disks += [pscustomobject]$entry
        continue
    }

    try {
        $mounted = Mount-VHD -Path $d.Path -ReadOnly -Passthru -ErrorAction Stop
        $entry.Mounted = $true
        Start-Sleep -Seconds 2
        # The partition types, which Windows reads on any disk: a Linux guest's XFS, ext4
        # or LVM carries no volume Windows can open, but its partitions say what they are.
        foreach ($part in @($mounted | Get-Disk | Get-Partition)) {
            $mbr = 0
            if ($null -ne $part.MbrType) { $mbr = [int]$part.MbrType }
            $entry.Partitions += [pscustomobject]@{
                GptType = "$($part.GptType)"; MbrType = $mbr; Size = [int64]$part.Size
            }
        }
        foreach ($vol in ($mounted | Get-Disk | Get-Partition | Get-Volume)) {
            $root = if ($vol.DriveLetter) { "$($vol.DriveLetter):" } else { "$($vol.Path)".TrimEnd('\') }
            $row = [ordered]@{
                FileSystem = "$($vol.FileSystem)"
                Label      = "$($vol.FileSystemLabel)"
                Size       = [int64]$vol.Size
                Free       = [int64]$vol.SizeRemaining
                Windows    = $false
                Hiberfil   = $false
                HiberfilSize = 0
                PageFile   = $false
                DirtyExit  = $null
                HiveLoadExit = $null
                ProductName = ''
                EditionID = ''
                InstallationType = ''
                CurrentBuildNumber = ''
                UBR = ''
                DisplayVersion = ''
            }
            if ($root) {
                $row.Windows = Test-Path -LiteralPath "$root\Windows\System32\config\SOFTWARE"
                $hib = "$root\hiberfil.sys"
                if (Test-Path -LiteralPath $hib) {
                    $row.Hiberfil = $true
                    try { $row.HiberfilSize = [int64](Get-Item -Force -LiteralPath $hib).Length } catch { }
                }
                $row.PageFile = Test-Path -LiteralPath "$root\pagefile.sys"
                if (Test-Path $fsutil) {
                    # The exit code, not the sentence: the text is localised, and a
                    # comparison against an English phrase reads every German host as
                    # clean. Measured: 0 on a volume that is not dirty.
                    $null = & $fsutil dirty query $root 2>&1
                    $row.DirtyExit = $LASTEXITCODE
                }

                # The guest's own registry, opened the way Windows opens an offline one.
                #
                # This replaces a guess. `Get-WindowsImage` leaves EditionId and
                # InstallationType empty on some disks, and concluding from that "the hive
                # is unreadable, the driver injection will fail" was wrong: measured on
                # such a disk, `reg load` succeeded and every value was there. So the hive
                # is opened rather than reasoned about — what comes back is real data, and
                # a load that fails is the only honest version of that warning, because
                # hivex would be facing the same hive.
                if ($row.Windows) {
                    $hive = "$root\Windows\System32\config\SOFTWARE"
                    $key = 'HKLM\PegaProxOffline'
                    $reg = Join-Path $env:SystemRoot 'System32\reg.exe'
                    $null = & $reg load $key $hive 2>&1
                    $row.HiveLoadExit = $LASTEXITCODE
                    if ($LASTEXITCODE -eq 0) {
                        $cv = "Registry::$key\Microsoft\Windows NT\CurrentVersion"
                        foreach ($name in 'ProductName','EditionID','InstallationType','CurrentBuildNumber','UBR','DisplayVersion') {
                            try { $row[$name] = "$((Get-ItemProperty -Path $cv -Name $name -ErrorAction Stop).$name)" } catch { }
                        }
                        # The handle has to go before the hive can be unloaded, and
                        # PowerShell holds one until the collector runs.
                        [gc]::Collect()
                        $null = & $reg unload $key 2>&1
                    }
                }
            }
            $entry.Volumes += [pscustomobject]$row
        }
    } catch {
        $entry.Error = $_.Exception.Message.Split([Environment]::NewLine)[0]
    } finally {
        # Always, on every path. A disk left attached is a VM that cannot start.
        try { Dismount-VHD -Path $d.Path -ErrorAction Stop } catch { }
        try { $entry.AttachedAfter = [bool](Get-VHD -Path $d.Path -ErrorAction Stop).Attached } catch { }
    }
    $result.Disks += [pscustomobject]$entry
}
$result.Inspected = $true
[pscustomobject]$result | ConvertTo-Json -Depth 6 -Compress
"""

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
    'VM_IMAGE_FACTS': VM_IMAGE_FACTS,
    'VM_DISK_INSPECTION': VM_DISK_INSPECTION,
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
#: No -Force and no -TurnOff. Forcing is an operator's decision, never a default: it is
#: what Proxmox calls "Force Stop" and puts behind its own menu entry, and on a migration
#: source it would leave the disks in the state an unexpected power cut leaves them in.
#:
#: Hyper-V asks for confirmation when the shutdown integration service does not answer, and
#: a non-interactive session cannot answer - the call then fails with "the host program
#: does not support user interaction" and nothing is shut down. That is a real condition,
#: measured on a guest whose service was not responding, and the answer is to check for it
#: first and say so plainly, not to suppress the question.
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

# Asked by its .NET type rather than its name: on a German host the service is called
# 'Herunterfahren' and a name comparison would find nothing and report every guest as
# unable to shut down.
# Select-Object -First 1 so the cast below always has one object: member access over a
# pipeline that matched twice yields an array, and [int] on an array throws in Windows
# PowerShell, which would end the script with no JSON for the caller to read.
$shutdown = Get-VMIntegrationService -VM $vm -ErrorAction SilentlyContinue |
    Where-Object { $_.GetType().Name -eq 'ShutdownComponent' } |
    Select-Object -First 1
# Compared on the numeric status, not the description: the description is localised.
if (-not $shutdown -or -not $shutdown.Enabled -or [int]$shutdown.PrimaryOperationalStatus -ne 2) {
    $why = if (-not $shutdown) { 'the guest has no shutdown integration service' }
           elseif (-not $shutdown.Enabled) { 'the shutdown integration service is disabled for this VM' }
           else { "the shutdown integration service reports '$($shutdown.PrimaryStatusDescription)'" }
    ConvertTo-Json -Depth 2 -Compress -InputObject ([pscustomobject]@{
        Id = $vm.Id.ToString(); State = $vm.State.ToString(); ShutdownRequested = $false
        Reason = "Cannot ask this guest to shut down: $why. Shut it down from inside the " +
                 "guest, or enable the integration service on the Hyper-V host. It was not " +
                 "forced off, because that would leave its disks as an unexpected power cut would."
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


#: Clear the automatic start of a source, and report what it was.
#:
#: Returning the previous value is the point: the migration records it and puts it back if
#: the run is abandoned, so a cancelled migration leaves the source exactly as it found it.
SET_AUTOMATIC_START = r'''
param(
    [Parameter(Mandatory=$true)][string]$VmId,
    [Parameter(Mandatory=$true)][string]$Action
)

$vm = Get-VM -Id $VmId -ErrorAction Stop
$previous = $vm.AutomaticStartAction.ToString()
$previousDelay = $vm.AutomaticStartDelay

if ($previous -ne $Action) {
    Set-VM -VM $vm -AutomaticStartAction $Action -ErrorAction Stop
}

ConvertTo-Json -Depth 2 -Compress -InputObject ([pscustomobject]@{
    Id       = $VmId
    Previous = $previous
    PreviousDelay = $previousDelay
    Current  = (Get-VM -Id $VmId).AutomaticStartAction.ToString()
})
'''
