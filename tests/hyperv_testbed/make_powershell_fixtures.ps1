# Run the product's own read-only scripts against stub cmdlets and save what they print.
#
# The output becomes the fixtures the Python normalisation is tested against. Their shape
# therefore comes from PowerShell's serialiser rather than from somebody's recollection of
# it: property names, array handling and -Depth behaviour are all exercised for real, and
# only the values are invented.
#
# Usage:  pwsh -NoProfile -File make_powershell_fixtures.ps1 [-OutDir <path>]

param([string]$OutDir)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutDir) { $OutDir = Join-Path $here 'ps_fixtures' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

. (Join-Path $here 'stub_cmdlets.ps1')

$scriptDir = Join-Path $OutDir '_scripts'
New-Item -ItemType Directory -Force -Path $scriptDir | Out-Null

# The product's scripts are exported to disk by the Python side before this runs, so this
# file never holds a second copy of them.
if (-not (Test-Path (Join-Path $scriptDir 'VM_INVENTORY.ps1'))) {
    throw "Export the product scripts into $scriptDir first (see conftest / export_scripts.py)."
}

function Save-Fixture {
    param([string]$Name, [string]$Json)
    $path = Join-Path $OutDir "$Name.json"
    Set-Content -Path $path -Value $Json -NoNewline -Encoding utf8
    Write-Output "wrote $Name.json"
}

# --- A clean generation 2 VM: one dynamic disk, one NIC, no checkpoints -----------------
$gen2 = New-StubVM -Id '11111111-1111-1111-1111-111111111111' -Name 'synthetic-gen2' `
    -Generation 2 -TpmEnabled $false -SecureBoot 'On' `
    -Disks @(New-StubDisk) `
    -Nics @(New-StubNic) `
    -Dvds @() -Checkpoints @()

# --- A generation 1 VM with two disks, two NICs and a checkpoint ------------------------
$gen1 = New-StubVM -Id '22222222-2222-2222-2222-222222222222' -Name 'synthetic-gen1' `
    -Generation 1 -ProcessorCount 2 -DynamicMemoryEnabled $false -TpmEnabled $false `
    -Disks @(
        (New-StubDisk -Path 'C:\VMs\gen1-os.vhdx' -ControllerType 'IDE' -VhdType 'Fixed'),
        (New-StubDisk -Path 'C:\VMs\gen1-data.vhdx' -ControllerType 'IDE' `
             -ControllerLocation 1 -VhdType 'Dynamic')
    ) `
    -Nics @(
        (New-StubNic -MacAddress '00155D000001' -SwitchName 'External'),
        (New-StubNic -MacAddress '00155D000002' -SwitchName 'Internal' -VlanMode 'Access' -VlanId 42)
    ) `
    -Checkpoints @((New-StubCheckpoint))

# --- A VM whose disk is a differencing file and whose second disk cannot be read --------
$awkward = New-StubVM -Id '33333333-3333-3333-3333-333333333333' -Name 'synthetic-awkward' `
    -State 'Running' -Generation 2 -TpmEnabled $true `
    -Disks @(
        (New-StubDisk -Path 'C:\VMs\awkward.avhdx' -VhdType 'Differencing' `
             -ParentPath 'C:\VMs\awkward.vhdx'),
        (New-StubDisk -Path 'C:\VMs\unreadable.vhdx' -ControllerLocation 1 -Readable $false)
    ) `
    -Nics @() -Checkpoints @((New-StubCheckpoint -Name 'Auto'), (New-StubCheckpoint -Name 'Manual'))

Set-StubVMs -VMs @($gen2, $gen1, $awkward)

Save-Fixture 'host_facts' (& (Join-Path $scriptDir 'HOST_FACTS.ps1'))
Save-Fixture 'vm_inventory' (& (Join-Path $scriptDir 'VM_INVENTORY.ps1'))
Save-Fixture 'vm_detail_gen2' (& (Join-Path $scriptDir 'VM_DETAIL.ps1') -VmId $gen2.Id.ToString())
Save-Fixture 'vm_detail_gen1' (& (Join-Path $scriptDir 'VM_DETAIL.ps1') -VmId $gen1.Id.ToString())
Save-Fixture 'vm_detail_awkward' (& (Join-Path $scriptDir 'VM_DETAIL.ps1') -VmId $awkward.Id.ToString())
Save-Fixture 'vm_state_gen2' (& (Join-Path $scriptDir 'VM_STATE.ps1') -VmId $gen2.Id.ToString())
Save-Fixture 'vm_state_awkward' (& (Join-Path $scriptDir 'VM_STATE.ps1') -VmId $awkward.Id.ToString())
Save-Fixture 'vm_disk_chain_gen2' (& (Join-Path $scriptDir 'VM_DISK_CHAIN.ps1') -VmId $gen2.Id.ToString())

# A single VM on the host, to prove one result still serialises as an array.
Set-StubVMs -VMs @($gen2)
Save-Fixture 'vm_inventory_single' (& (Join-Path $scriptDir 'VM_INVENTORY.ps1'))

# No VMs at all, which must be [] rather than nothing.
Set-StubVMs -VMs @()
Save-Fixture 'vm_inventory_empty' (& (Join-Path $scriptDir 'VM_INVENTORY.ps1'))

Write-Output "fixtures in $OutDir"
