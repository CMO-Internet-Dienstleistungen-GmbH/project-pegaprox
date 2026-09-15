#!/usr/bin/env python3
"""Read-only connectivity and capability probe for a Hyper-V migration source.

Part of the Hyper-V migration epic (fork issue #15, spike issue #16). The probe
answers three questions that the epic must not guess at:

1. Does the intended PegaProx client path reach the host at all, and does it
   return VM and disk metadata?
2. When it fails, is the cause missing certificate trust, failed authentication
   or missing Hyper-V rights? These are separate operator problems and must not
   collapse into one "connection failed".
3. Which host, PowerShell, Hyper-V and guest versions are actually present?

The probe never changes the host. It runs only Get-* cmdlets and reads no guest
data. Credentials arrive through the environment or stdin, never through argv,
because argv is world-readable via the process list.

Usage:
    export HYPERV_PROBE_PASSWORD=...   # or omit and be prompted on a tty
    ./misc/hyperv_connectivity_check.py --host <fqdn> --user <account>

Requires pypsrp (https://pypi.org/project/pypsrp/), which is not a PegaProx
runtime dependency. Install it into a throwaway environment for the probe.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

PASSWORD_ENV_VAR = "HYPERV_PROBE_PASSWORD"
REDACTION_PLACEHOLDER = "***"

# The probe classifies failures exactly as the product does. Importing rather than copying
# keeps one definition: a probe that disagreed with the running code about why a host said
# no would be worse than no probe. The repository root is added to the path so this script
# still runs from a checkout without PegaProx being installed.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from pegaprox.core.hyperv_errors import (  # noqa: E402
    KIND_OK, KIND_UNREACHABLE, KIND_TIMEOUT, KIND_CERTIFICATE, KIND_AUTHENTICATION,
    KIND_AUTHORIZATION, KIND_MISSING_FEATURE, KIND_CLIENT_DEPENDENCY, KIND_UNKNOWN,
    classify as classify_failure, remedy as _remedy_for,
)


@dataclass
class ProbeResult:
    """Outcome of a single probe step, safe to print."""

    name: str
    kind: str
    detail: str = ""
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.kind == KIND_OK

    def remedy(self) -> str:
        return _remedy_for(self.kind)


@dataclass
class Redactor:
    """Removes known secret values from anything on its way to an output stream."""

    secrets: list[str] = field(default_factory=list)

    def add(self, secret: str | None) -> None:
        # Single characters would redact half the output; they are never real secrets.
        if secret and len(secret) > 1:
            self.secrets.append(secret)

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTION_PLACEHOLDER)
        return text


def classify_failure(exc_type_name: str, message: str) -> str:
    """Map a transport or remoting failure onto one operator-actionable kind.

    Classification reads the exception type first and the message only as a
    fallback, because message wording varies between pypsrp, requests and the
    Windows side. A message that matches nothing stays KIND_UNKNOWN rather than
    being forced into a neighbouring category.
    """
    lowered = message.lower()

    # A missing client library is the most common first failure and says nothing about the
    # host, so it must not be reported as an unknown remote error.
    if exc_type_name in ("ModuleNotFoundError", "ImportError"):
        return KIND_CLIENT_DEPENDENCY
    if "ssl" in exc_type_name.lower() or "certificate verify failed" in lowered:
        return KIND_CERTIFICATE
    if "certificate" in lowered and "verify" in lowered:
        return KIND_CERTIFICATE
    # A read timeout is not unreachability: the endpoint accepted the connection and then
    # stayed silent, which sends the operator to the host's timeouts, not to the firewall.
    if exc_type_name == "ReadTimeout":
        return KIND_TIMEOUT
    if exc_type_name in ("ConnectionError", "ConnectTimeout", "NewConnectionError"):
        return KIND_UNREACHABLE
    if any(marker in lowered for marker in ("name or service not known", "connection refused")):
        return KIND_UNREACHABLE
    if "authenticationerror" in exc_type_name.lower():
        return KIND_AUTHENTICATION
    # WinRM carries authorization faults over HTTP 401, so a message can hold both an
    # "access is denied" and an "unauthorized". The rights marker is the more specific of
    # the two and has to win, or missing group membership reads as a bad password.
    if any(marker in lowered for marker in ("access is denied", "access denied", "5 (0x5)", "not authorized", "403")):
        return KIND_AUTHORIZATION
    if any(marker in lowered for marker in ("401", "unauthorized", "logon failure", "spnego", "ntlm")):
        return KIND_AUTHENTICATION
    if any(marker in lowered for marker in ("is not recognized", "commandnotfound", "hyper-v module")):
        return KIND_MISSING_FEATURE
    return KIND_UNKNOWN


def _probe(name: str, action: Callable[[], Any]) -> ProbeResult:
    """Run one read-only step and turn any failure into a classified result."""
    try:
        return ProbeResult(name=name, kind=KIND_OK, data=action())
    except Exception as exc:  # noqa: BLE001 - the probe classifies, it does not recover
        message = str(exc)
        return ProbeResult(
            name=name,
            kind=classify_failure(type(exc).__name__, message),
            detail=message,
        )


def normalize_vm(raw: dict) -> dict:
    """Reduce a Hyper-V VM object to the fields the migration epic needs.

    Only migration-relevant hardware is kept. Notes and custom fields are
    dropped because they can carry guest or customer data the probe has no
    reason to read.
    """
    return {
        "id": raw.get("Id"),
        "name": raw.get("Name"),
        "state": raw.get("State"),
        "generation": raw.get("Generation"),
        "cpu_count": raw.get("ProcessorCount"),
        "memory_startup_bytes": raw.get("MemoryStartup"),
        "dynamic_memory_enabled": raw.get("DynamicMemoryEnabled"),
        "checkpoint_count": raw.get("CheckpointCount"),
        "version": raw.get("Version"),
    }


def normalize_disk(raw: dict) -> dict:
    """Reduce a VHD object to what preflight has to reason about."""
    return {
        "path": raw.get("Path"),
        "vhd_format": raw.get("VhdFormat"),
        "vhd_type": raw.get("VhdType"),
        "parent_path": raw.get("ParentPath"),
        "file_size": raw.get("FileSize"),
        "size": raw.get("Size"),
        "attached": raw.get("Attached"),
    }


# Each script is read-only: Get-* only, no state change, no guest access.
#
# The collection scripts pass their results through -InputObject @(...) rather than piping
# them. Three things force that shape, all of them measured against a real parser:
#
#   -AsArray would be the obvious switch and does not exist in Windows PowerShell 5.1,
#   which is what a Hyper-V host normally runs; passing it fails the whole call.
#
#   Piping @(...) into ConvertTo-Json does not help either, because the pipeline unrolls
#   the array again and a single result still serialises as a bare object.
#
#   -InputObject @(...) is the form that actually yields [] for no results, [{...}] for
#   one and [{...},{...}] for many, on every version.
#
# -Depth is explicit because the 5.1 default of 2 truncates nested values in silence.
_PS_HOST_FACTS = r"""
$os = Get-CimInstance Win32_OperatingSystem
[pscustomobject]@{
    OSCaption         = $os.Caption
    OSVersion         = $os.Version
    PSVersion         = $PSVersionTable.PSVersion.ToString()
    HyperVModule      = (Get-Module -ListAvailable Hyper-V | Select-Object -First 1).Version.ToString()
} | ConvertTo-Json -Compress -Depth 4
"""

_PS_VM_INVENTORY = r"""
$items = Get-VM | ForEach-Object {
    [pscustomobject]@{
        Id                   = $_.Id.ToString()
        Name                 = $_.Name
        State                = $_.State.ToString()
        Generation           = $_.Generation
        ProcessorCount       = $_.ProcessorCount
        MemoryStartup        = $_.MemoryStartup
        DynamicMemoryEnabled = $_.DynamicMemoryEnabled
        CheckpointCount      = (Get-VMSnapshot -VMName $_.Name -ErrorAction SilentlyContinue | Measure-Object).Count
        Version              = $_.Version
    }
}
ConvertTo-Json -InputObject @($items) -Compress -Depth 4
"""

_PS_DISK_INVENTORY = r"""
$items = Get-VM | Get-VMHardDiskDrive | ForEach-Object {
    $vhd = Get-VHD -Path $_.Path -ErrorAction Stop
    [pscustomobject]@{
        Path       = $vhd.Path
        VhdFormat  = $vhd.VhdFormat.ToString()
        VhdType    = $vhd.VhdType.ToString()
        ParentPath = $vhd.ParentPath
        FileSize   = $vhd.FileSize
        Size       = $vhd.Size
        Attached   = $vhd.Attached
    }
}
ConvertTo-Json -InputObject @($items) -Compress -Depth 4
"""


def _as_list(parsed: Any) -> list[dict]:
    """Defend against a host that serialises a single result as a bare object anyway."""
    if parsed is None:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


class HyperVProbe:
    """Runs the read-only probe sequence over PowerShell Remoting."""

    def __init__(self, host: str, user: str, password: str, port: int, verify: bool) -> None:
        self._host = host
        self._user = user
        self._password = password
        self._port = port
        self._verify = verify

    def _run_script(self, script: str) -> Any:
        from pypsrp.powershell import PowerShell, RunspacePool
        from pypsrp.wsman import WSMan

        wsman = WSMan(
            self._host,
            port=self._port,
            username=self._user,
            password=self._password,
            ssl=True,
            auth="ntlm",
            cert_validation=self._verify,
        )
        with wsman, RunspacePool(wsman) as pool:
            powershell = PowerShell(pool)
            powershell.add_script(script)
            output = powershell.invoke()
            if powershell.had_errors:
                raise RuntimeError("; ".join(str(err) for err in powershell.streams.error))
        # PSRP can split a long string across output records; joining first keeps a large
        # inventory from failing as a parse error instead of returning data.
        return json.loads("".join(str(part) for part in output)) if output else None

    def run(self) -> list[ProbeResult]:
        """Execute the probe steps in dependency order and return every result.

        Steps are not aborted on the first failure: a host that authenticates
        but lacks Hyper-V rights should report exactly that, and the operator
        should see the whole picture in one run.
        """
        facts = _probe("host_facts", lambda: self._run_script(_PS_HOST_FACTS))
        results = [facts]

        if not facts.ok:
            return results

        vms = _probe("vm_inventory", lambda: [normalize_vm(v) for v in _as_list(self._run_script(_PS_VM_INVENTORY))])
        results.append(vms)

        disks = _probe(
            "disk_inventory",
            lambda: [normalize_disk(d) for d in _as_list(self._run_script(_PS_DISK_INVENTORY))],
        )
        results.append(disks)
        return results


def render(results: list[ProbeResult], redact: Redactor) -> str:
    """Format probe results for a terminal, with every secret removed."""
    lines = []
    for result in results:
        status = "OK  " if result.ok else "FAIL"
        lines.append(f"[{status}] {result.name}: {result.kind}")
        if result.detail:
            lines.append(f"       detail: {redact(result.detail)}")
            lines.append(f"       remedy: {result.remedy()}")
        if result.data is not None:
            rendered = json.dumps(result.data, indent=2, default=str)
            lines.append("\n".join(f"       {line}" for line in redact(rendered).splitlines()))
    return "\n".join(lines)


def read_password(redact: Redactor) -> str:
    """Take the password from the environment, or prompt when attached to a tty."""
    password = os.environ.get(PASSWORD_ENV_VAR)
    if not password and sys.stdin.isatty():
        password = getpass.getpass(f"Password for the Hyper-V account ({PASSWORD_ENV_VAR} not set): ")
    if not password:
        raise SystemExit(f"No password available. Set {PASSWORD_ENV_VAR} or run on a terminal.")
    redact.add(password)
    return password


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="Hyper-V host name that matches its WinRM certificate")
    parser.add_argument("--user", required=True, help="Account in 'Hyper-V Administrators' and 'Remote Management Users'")
    parser.add_argument("--port", type=int, default=5986, help="WinRM HTTPS port (default: 5986)")
    parser.add_argument(
        "--insecure-skip-verify",
        action="store_true",
        help="Skip certificate validation. Use only to prove that trust is the failing part.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    redact = Redactor()
    password = read_password(redact)

    # The host name and the account name are exactly the values that may not be written
    # down here, and unlike the host's own name these are known to the probe. Removing them
    # makes a captured run publishable without a second editing pass.
    redact.add(args.host)
    redact.add(args.user)

    probe = HyperVProbe(
        host=args.host,
        user=args.user,
        password=password,
        port=args.port,
        verify=not args.insecure_skip_verify,
    )
    results = probe.run()
    print(render(results, redact))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
