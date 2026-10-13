"""Run every parameterised Hyper-V script through a real PowerShell, the way the product does.

This file exists because of a defect the rest of the suite could not see. The manager used
to bind values by splicing `$VmId = '...'` into the script text after its param() block.
Against a test double that never runs PowerShell, and against `make_powershell_fixtures.ps1`
— which invokes the scripts as files with real parameters — that looked correct. Against a
real host every one of them failed:

    Cannot process command because of one or more missing mandatory parameters: VmId.

A [Parameter(Mandatory=$true)] is demanded when the param() block is entered, before any
assignment in the body can run. The fix was to bind parameters instead of splicing text,
and this is the test that would have caught it.

It needs `pwsh`, not Windows and not Hyper-V: the failure is in PowerShell's parameter
binding, which behaves the same everywhere. The scripts will fail here for want of `Get-VM`
and that is expected — what must not appear is a *binding* error, because that means the
value never arrived at all.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from pegaprox.core import hyperv_scripts as scripts

pwsh = shutil.which('pwsh')
pytestmark = pytest.mark.skipif(pwsh is None, reason='needs pwsh to run PowerShell')

GUID = '11111111-2222-3333-4444-555555555555'

#: Each script the manager calls with parameters, and a plausible set of values. Kept
#: explicit rather than derived from the module, so adding a parameterised script to the
#: product without adding it here shows up as a gap instead of silently passing.
#:
#: SHUTDOWN_VM gets a one-second timeout for a reason that is about this harness, not about
#: the product: without Hyper-V's cmdlets its polling loop never sees a stopped VM, so it
#: would sit in Start-Sleep for the whole timeout it is given.
PARAMETERISED = {
    'VM_DETAIL': {'VmId': GUID},
    'VM_STATE': {'VmId': GUID},
    'VM_MERGE_STATE': {'VmId': GUID},
    'VM_DISK_CHAIN': {'VmId': GUID},
    'ISO_LIBRARY': {'Paths': ['C:\\iso', 'D:\\library']},
    'START_VM': {'VmId': GUID},
    'SHUTDOWN_VM': {'VmId': GUID, 'TimeoutSeconds': 1},
    'REMOVE_CHECKPOINTS': {'VmId': GUID, 'CheckpointName': '', 'All': True},
    'MOUNT_ISO': {'VmId': GUID, 'IsoPath': 'C:\\iso\\x.iso'},
    'EJECT_ISO': {'VmId': GUID},
}

#: What PowerShell says when a value did not reach the param() block. Both wordings, because
#: a developer machine may be running a localised PowerShell.
BINDING_ERRORS = ('missing mandatory parameters', 'erforderliche Parameter fehlen')

#: The shape the product used to produce. Kept as a case of its own so the harness proves it
#: can still see the defect: a test that cannot fail is not evidence of anything.
SPLICED_AS_THE_PRODUCT_ONCE_DID = (
    'param([Parameter(Mandatory=$true)][string]$VmId)\n'
    f"$VmId = '{GUID}'\n"
    '"got: $VmId"')

#: A value that would be code if it were spliced instead of bound. 23 characters.
AWKWARD = "x'; Remove-VM -Name * #"
LENGTH_CHECK = ('param([Parameter(Mandatory=$true)][string]$VmId)\n'
                'if ($VmId.Length -ne 23) { Write-Error "length was $($VmId.Length)" }')

# The product calls AddScript(text).AddParameter(name, value) on a runspace. This harness
# does the same through the same automation API, so it exercises the product's path and not
# the file-with-arguments path the fixtures use. Every case runs in one process: starting
# pwsh costs several seconds and there are a dozen of them.
HARNESS = r'''
$spec = [Console]::In.ReadToEnd() | ConvertFrom-Json
$results = @()
foreach ($case in $spec.Cases) {
    $ps = [PowerShell]::Create()
    $null = $ps.AddScript($case.Script)
    if ($case.Parameters) {
        foreach ($p in $case.Parameters.PSObject.Properties) {
            $null = $ps.AddParameter($p.Name, $p.Value)
        }
    }
    $messages = @()
    # A binding failure is thrown rather than written to the error stream, so swallowing it
    # here would hide exactly what this harness exists to detect.
    try { $null = $ps.Invoke() } catch { $messages += $_.Exception.Message }
    $messages += @($ps.Streams.Error | ForEach-Object { $_.ToString() })
    $ps.Dispose()
    $results += [pscustomobject]@{ Name = $case.Name; Errors = @($messages) }
}
ConvertTo-Json -Depth 5 -Compress -InputObject ([pscustomobject]@{ Results = $results })
'''


@pytest.fixture(scope='module')
def errors_by_case():
    """Every case run once, in one pwsh process, keyed by name."""
    cases = [{'Name': name, 'Script': getattr(scripts, name), 'Parameters': values}
             for name, values in sorted(PARAMETERISED.items())]
    cases.append({'Name': 'spliced', 'Script': SPLICED_AS_THE_PRODUCT_ONCE_DID,
                  'Parameters': None})
    cases.append({'Name': 'awkward', 'Script': LENGTH_CHECK, 'Parameters': {'VmId': AWKWARD}})

    assert pwsh is not None  # guaranteed by the module-level skipif
    # The payload goes over stdin, not the command line: a script full of quotes and braces
    # on argv is parsed by pwsh before the harness ever sees it.
    with tempfile.NamedTemporaryFile('w', suffix='.ps1', delete=False) as handle:
        handle.write(HARNESS)
        harness_path = handle.name
    try:
        result = subprocess.run(
            [pwsh, '-NoProfile', '-NonInteractive', '-File', harness_path],
            input=json.dumps({'Cases': cases}), capture_output=True, text=True, timeout=300)
    finally:
        os.unlink(harness_path)

    stdout = result.stdout.strip()
    if not stdout:
        pytest.fail(f'pwsh produced nothing; stderr was {result.stderr[:300]}')
    parsed = json.loads(stdout.splitlines()[-1])['Results']
    return {entry['Name']: entry['Errors'] or [] for entry in parsed}


def _binding_errors(messages):
    return [m for m in messages if any(marker in m for marker in BINDING_ERRORS)]


@pytest.mark.parametrize('name', sorted(PARAMETERISED))
def test_every_parameterised_script_receives_its_values(errors_by_case, name):
    failed = _binding_errors(errors_by_case[name])
    assert not failed, f'{name} did not receive its parameters: {failed}'


def test_the_old_splicing_approach_would_fail_here(errors_by_case):
    """The guard on the guard: this harness has to be able to see the defect."""
    assert _binding_errors(errors_by_case['spliced']), (
        f"a spliced value should still be refused, got {errors_by_case['spliced']}")


def test_a_value_that_looks_like_code_arrives_as_one_string(errors_by_case):
    """The reason parameters beat splicing, stated as a test.

    Nothing escapes this value any more, because nothing turns it into source.
    """
    assert not errors_by_case['awkward'], (
        f"the value did not arrive intact: {errors_by_case['awkward']}")
