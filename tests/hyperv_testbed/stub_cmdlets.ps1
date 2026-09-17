# Stand-ins for the Hyper-V module, so the product's own scripts can be executed rather
# than only parsed.
#
# There is no Hyper-V outside Windows, so the alternative to this is a hand-written JSON
# fixture — which only ever proves that the fixture and the code agree with each other.
# Running the real scripts against objects shaped like the real ones puts PowerShell's own
# serialiser in the loop: the property names, the array handling and the depth behaviour
# are exercised, and only the values are invented.
#
# What this cannot prove: that Hyper-V's objects really carry these members. That needs a
# host, and is recorded as the open blocker in docs/hyperv-verification.md.

$global:StubVMs = @()

function New-StubVM {
    param(
        [string]$Id = '11111111-1111-1111-1111-111111111111',
        [string]$Name = 'synthetic-gen2',
        [string]$State = 'Off',
        [int]$Generation = 2,
        [int]$ProcessorCount = 4,
        [long]$MemoryStartup = 4294967296,
        [long]$MemoryMinimum = 2147483648,
        [long]$MemoryMaximum = 8589934592,
        [bool]$DynamicMemoryEnabled = $true,
        [bool]$TpmEnabled = $false,
        [string]$SecureBoot = 'On',
        [array]$Disks = @(),
        [array]$Nics = @(),
        [array]$Dvds = @(),
        [array]$Checkpoints = @()
    )
    [pscustomobject]@{
        Id                   = [guid]$Id
        Name                 = $Name
        State                = $State
        Generation           = $Generation
        ProcessorCount       = $ProcessorCount
        MemoryStartup        = $MemoryStartup
        MemoryMinimum        = $MemoryMinimum
        MemoryMaximum        = $MemoryMaximum
        DynamicMemoryEnabled = $DynamicMemoryEnabled
        Version              = '11.0'
        Uptime               = [timespan]::FromMinutes(0)
        # Carried on the object so the per-VM cmdlets below can find them.
        _TpmEnabled          = $TpmEnabled
        _SecureBoot          = $SecureBoot
        _Disks               = $Disks
        _Nics                = $Nics
        _Dvds                = $Dvds
        _Checkpoints         = $Checkpoints
    }
}

function New-StubDisk {
    param(
        [string]$Path = 'C:\VMs\synthetic.vhdx',
        [string]$ControllerType = 'SCSI',
        [int]$ControllerNumber = 0,
        [int]$ControllerLocation = 0,
        [string]$VhdType = 'Dynamic',
        [string]$ParentPath = $null,
        [long]$Size = 45097156608,
        [long]$FileSize = 8388608,
        [bool]$Readable = $true
    )
    [pscustomobject]@{
        Path = $Path; ControllerType = $ControllerType
        ControllerNumber = $ControllerNumber; ControllerLocation = $ControllerLocation
        _VhdType = $VhdType; _ParentPath = $ParentPath; _Size = $Size
        _FileSize = $FileSize; _Readable = $Readable
    }
}

function New-StubNic {
    param(
        [string]$Name = 'Network Adapter',
        [string]$MacAddress = '00155D000001',
        [bool]$DynamicMacAddressEnabled = $false,
        [string]$SwitchName = 'External',
        [bool]$Connected = $true,
        [string]$VlanMode = 'Untagged',
        [int]$VlanId = 0
    )
    [pscustomobject]@{
        Name = $Name; MacAddress = $MacAddress
        DynamicMacAddressEnabled = $DynamicMacAddressEnabled
        SwitchName = $SwitchName; Connected = $Connected
        _VlanMode = $VlanMode; _VlanId = $VlanId
    }
}

function New-StubCheckpoint {
    param(
        [string]$Name = 'Before update',
        [string]$SnapshotType = 'Standard',
        [string]$ParentSnapshotName = $null
    )
    [pscustomobject]@{
        Name = $Name; SnapshotType = $SnapshotType
        CreationTime = [datetime]'2026-09-01T10:00:00Z'
        ParentSnapshotName = $ParentSnapshotName
    }
}

function Set-StubVMs {
    # Global rather than script scope: the product's scripts are invoked with & and run in
    # a child scope, where a script-scoped variable of this file would not be visible.
    # AllowEmptyCollection: a host with no VMs is a case the scripts must handle, so it
    # has to be expressible here.
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][array]$VMs)
    $global:StubVMs = $VMs
}

# ---------------------------------------------------------------------------
# The cmdlets the product's scripts call
# ---------------------------------------------------------------------------

function Get-VM {
    param([string]$Id, [string]$Name, [switch]$ErrorAction)
    if ($Id) {
        $match = @($global:StubVMs | Where-Object { $_.Id.ToString() -eq $Id })
        if ($match.Count -eq 0) { throw "Hyper-V was unable to find a virtual machine with id $Id." }
        return $match[0]
    }
    if ($Name) { return @($global:StubVMs | Where-Object { $_.Name -eq $Name }) }
    return $global:StubVMs
}

function Get-VMHost { [pscustomobject]@{ ErrorState = 'OK' } }

function Get-VMHardDiskDrive { param($VM) return $VM._Disks }

function Get-VMNetworkAdapter { param($VM, $VMNetworkAdapter) return $VM._Nics }

function Get-VMNetworkAdapterVlan {
    param($VMNetworkAdapter)
    [pscustomobject]@{
        OperationMode = $VMNetworkAdapter._VlanMode
        AccessVlanId  = $VMNetworkAdapter._VlanId
    }
}

function Get-VMDvdDrive { param($VM) return $VM._Dvds }

function Get-VMSnapshot { param($VM, $VMName) return $VM._Checkpoints }

function Get-VMFirmware {
    param($VM)
    [pscustomobject]@{
        SecureBoot = $VM._SecureBoot
        SecureBootTemplate = 'MicrosoftWindows'
        BootOrder = @(
            [pscustomobject]@{ BootType = 'Drive' },
            [pscustomobject]@{ BootType = 'Network' }
        )
    }
}

function Get-VMBios {
    param($VM)
    [pscustomobject]@{ StartupOrder = @('CD', 'IDE', 'LegacyNetworkAdapter', 'Floppy') }
}

function Get-VMSecurity { param($VM) [pscustomobject]@{ TpmEnabled = $VM._TpmEnabled } }

function Get-VHD {
    param([string]$Path)
    $disk = $null
    foreach ($vm in $global:StubVMs) {
        foreach ($candidate in $vm._Disks) {
            if ($candidate.Path -eq $Path) { $disk = $candidate; break }
        }
        if ($disk) { break }
    }
    if (-not $disk) { throw "The system cannot find the file '$Path'." }
    if (-not $disk._Readable) { throw "Access is denied. '$Path'" }
    [pscustomobject]@{
        Path = $disk.Path
        VhdFormat = 'VHDX'
        VhdType = $disk._VhdType
        ParentPath = $disk._ParentPath
        FileSize = $disk._FileSize
        Size = $disk._Size
        Attached = $true
        DiskIdentifier = 'a1b2c3d4-0000-0000-0000-000000000000'
    }
}

function Get-CimInstance {
    param([string]$ClassName)
    [pscustomobject]@{
        Caption = 'Microsoft Windows Server 2022 Standard'
        Version = '10.0.20348'
    }
}
