# Hyper-V migration: test path and access contract

Scope: the verification contract for the Hyper-V migration source described in
fork issue #15, established by its first sub-issue #16. It records what the
implementation may rely on, what has to be measured on a real host, and which
of those measurements are still outstanding. The per-combination result of those
measurements is kept in `docs/hyperv-compatibility.md`.

Nothing here names a real host, account, network or customer. Every value that
identifies an environment is a placeholder; every value that describes the
product or its load is given as measured.

The probe enforces part of that itself rather than leaving it to whoever pastes
a run into this file. It removes the host name and the account name from its own
output, because the operator passed both on the command line and it therefore
knows what to strip. It never asks the host for `$env:COMPUTERNAME`: that is the
one name the probe could learn and could not remove, since nothing on this side
knows it in advance. Output captured from a run is publishable as it stands.

## Status

The client path, the host prerequisites and the probe procedure below are
established. The live measurements are **not**: no Hyper-V test host has been
released for this work yet.

| Item | State |
|---|---|
| Client library and transport decided | Established; `pypsrp` 0.9.1 installed and exercised |
| Host prerequisites and rights identified | Established, from vendor documentation |
| `unreachable` and `certificate` told apart over the real transport | Proven locally, see below |
| Remaining failure classes distinguishable | Proven by `tests/test_hyperv_connectivity_check.py` |
| Read-only connection against a real host | **Blocked** — no released test system |
| Host, PowerShell, Hyper-V, guest and Proxmox versions | **Blocked** — must be measured, not assumed |

The blocked rows are the acceptance criteria of #16 that need a released
system. A successful run against a mock does not substitute for them.

## Client path

PegaProx runs on Linux, so the Hyper-V host is reached over PowerShell
Remoting, not through a Windows-only management API.

| Choice | Value | Why |
|---|---|---|
| Library | `pypsrp` 0.9.1 (released 2026-03-16, requires Python >= 3.10) | Pure-Python PSRP and WSMan client; no Windows or system library needed on the PegaProx side |
| Transport | WSMan over HTTPS, port 5986 | The plaintext listener is not used at all |
| Authentication | NTLM via `pyspnego` | Works for standalone hosts without a domain join or Kerberos realm on the Linux side |
| Certificate handling | Validated against the system trust store | Skipping validation is a diagnostic flag, never a configuration |

`pypsrp` depends on `cryptography >= 3.1`, `pyspnego >= 0.7.0` and
`requests >= 2.27.0`. The PegaProx pin of `cryptography >= 50.0.0, < 52`
satisfies that lower bound, so adding the library does not move the existing
crypto pins. It is an optional dependency in the same sense as `pyvmomi` and
`XenAPI`: absent, the Hyper-V source is unavailable and the rest of PegaProx is
unaffected.

## Host prerequisites

These are the host-side conditions the probe checks. They follow the vendor
documentation for remote Hyper-V management and have not yet been confirmed
against a live host.

- **WinRM HTTPS listener on 5986**, reachable from the PegaProx machine.
- **A listener certificate whose subject matches the name PegaProx connects
  with.** Connecting by address when the certificate names the host is a
  trust failure, and the probe reports it as one.
- **The issuing CA present in the PegaProx machine's trust store.** This is
  the most common first failure and is deliberately not worked around.
- **The account in both the local `Hyper-V Administrators` group and the local
  `Remote Management Users` group.** The first grants Hyper-V operations, the
  second grants WinRM access. Either one alone fails, and the two fail
  differently.
- **The Hyper-V role and its PowerShell module installed**, so `Get-VM`,
  `Get-VMHardDiskDrive`, `Get-VHD` and `Get-VMSnapshot` resolve.

Read access to the VHDX files is a separate question from Hyper-V rights and
belongs to sub-issue #20. `Get-VHD` reports metadata; it does not prove the
migration can read the file bytes.

## Versions to measure

The implementation must not guess these. `misc/hyperv_connectivity_check.py`
reads the first four; the remaining rows are recorded by hand when the test
combination is released.

| Value | Source | Measured |
|---|---|---|
| Host OS caption and build | `Win32_OperatingSystem` | outstanding |
| Windows PowerShell version | `$PSVersionTable.PSVersion` | outstanding |
| Hyper-V PowerShell module version | `Get-Module -ListAvailable Hyper-V` | outstanding |
| VM configuration version | `Get-VM` | outstanding |
| Guest OS of the synthetic test VM | recorded by hand | outstanding |
| Proxmox VE version of the migration target | recorded by hand | outstanding |
| `pypsrp` version actually installed | `pip show pypsrp` | outstanding |

## Test roles

Four roles, described by function rather than by name. Real names, addresses
and accounts stay out of this repository entirely.

| Role | Description |
|---|---|
| `<hyperv-host>` | A standalone Hyper-V host, not a failover cluster member. Clustered hosts are out of scope for the whole epic. |
| `<hyperv-account>` | A dedicated account for PegaProx, in the two groups named above and in no others. |
| `<synthetic-guest>` | A Generation 2 VM with one VHDX and one NIC, holding no real data. Powered off for any migration test. |
| `<pve-target>` | A Proxmox VE cluster or node that may receive throwaway VMs. |

The guest is synthetic by construction: it is created for the test, never
copied from an existing workload. That is what makes the transfer evidence
publishable in an anonymised form.

## Reproducible checks

`misc/hyperv_connectivity_check.py` is the read-only probe. It runs `Get-*`
cmdlets only, changes nothing on the host, and reads no guest data.

```bash
python3 -m venv /tmp/hyperv-probe && /tmp/hyperv-probe/bin/pip install pypsrp
export HYPERV_PROBE_PASSWORD='…'          # or omit and answer the prompt
/tmp/hyperv-probe/bin/python misc/hyperv_connectivity_check.py \
    --host '<hyperv-host>' --user '<hyperv-account>'
```

The password is read from the environment or from a terminal prompt and never
from the command line, because arguments are readable in the process list. Any
value it holds is removed from every line the probe prints.

The probe runs three steps and reports all reachable ones rather than stopping
at the first failure, so a host that authenticates but lacks Hyper-V rights
says exactly that:

1. **`host_facts`** — OS, PowerShell and Hyper-V module versions. Its failure
   ends the run, because the later steps would only repeat the same cause.
2. **`vm_inventory`** — VM identity, state, generation, CPU, startup memory,
   dynamic memory flag and checkpoint count.
3. **`disk_inventory`** — VHDX path, format, type, parent path, file size and
   attachment state. This is the step that fails when the account has WinRM
   access but not Hyper-V rights.

### The three failure classes

The point of the probe is that these never collapse into one message. Each
sends the operator somewhere different:

| Reported kind | What it means | Where to look |
|---|---|---|
| `unreachable` | No WSMan endpoint answered | Listener, port 5986, firewall |
| `timeout` | The endpoint accepted the connection and then stayed silent | The WSMan operation timeout, and how long the cmdlet takes on the host |
| `certificate` | The endpoint answered, its certificate was not trusted | CA in the client trust store, or the listener certificate's subject |
| `authentication` | Trust is fine, the credentials were rejected | Account, password, NTLM accepted by WinRM |
| `authorization` | The account is known but not permitted | The two group memberships |
| `missing_feature` | The account is permitted, the cmdlet is absent | Hyper-V role and module on the host |
| `client_dependency` | The probe never left the PegaProx machine | `pypsrp` missing in the local environment |
| `unknown` | Nothing matched | The raw message, printed verbatim |

`--insecure-skip-verify` exists to prove that certificate trust is the failing
part, by making the same call succeed without validation. It is a diagnostic
step in this document and never a configuration in the product.

### Evidence without a host

Two of the failure classes were provoked over the real client path, with
`pypsrp` 0.9.1 installed and no mocking, against loopback endpoints.

**No listener** — nothing accepts the connection:

```
[FAIL] host_facts: unreachable
       detail: HTTPSConnectionPool(host='***', port=5986): … Connection refused
       remedy: Check WinRM HTTPS listener, port 5986 and any firewall in between.
```

**A TLS endpoint with an untrusted certificate** — the connection succeeds and
only the trust chain fails:

```
[FAIL] host_facts: certificate
       detail: … [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate
       remedy: Install the host's issuing CA on the PegaProx machine, or correct the listener certificate.
```

Repeating that second run with `--insecure-skip-verify` gets past the trust
failure and fails later for a different reason, which is what shows the
`certificate` verdict was about trust and not about reachability.

Everything else — authentication against authorization in particular, and
password redaction — is behaviour rather than connectivity and is guarded by
`tests/test_hyperv_connectivity_check.py`. 45 tests, network-free, no Hyper-V
host required:

```
45 passed
```

They prove that the operator-facing causes stay distinct, that a password handed
to the probe never appears in its output, and that the embedded PowerShell stays
inside what the host can run. They do not prove that a Hyper-V host answers, and
they do not prove the `authorization` path against a real account. Only a
released test system does that.

### What the embedded PowerShell may use

Windows Server runs Windows PowerShell 5.1 by default, and the probe is written
for it rather than for PowerShell 7. Its `ConvertTo-Json` accepts only
`-InputObject`, `-Depth` and `-Compress`. `-AsArray` arrived in PowerShell 6 and
fails the entire call with a parameter error on 5.1, so a probe using it would
report every host as broken for a reason that has nothing to do with the host.

Getting a JSON array without that switch is less obvious than it looks, and the
shape was measured against a real parser rather than reasoned about:

| Form | One result |
|---|---|
| `… \| ConvertTo-Json` | `{…}` — a bare object |
| `@(…) \| ConvertTo-Json` | `{…}` — the pipeline unrolls the array again |
| `ConvertTo-Json -InputObject @(…)` | `[{…}]` |

Only the third form holds for every count: `[]` for none, `[{…}]` for one and a
full array for many. The collection scripts use it, and the Python side keeps a
defensive unwrap for a host that answers with a bare object anyway.

`-Depth` is always explicit, because the 5.1 default of 2 truncates nested
values without raising anything and yields a half-read object that looks
complete.

All three scripts parse cleanly under the PowerShell language parser, and tests
hold both the serialisation form and the rule that the probe's scripts contain
no mutating cmdlet.

## Recording a run

When a test combination is released, the outcome is added here as: the version
table above filled in, the probe output with placeholders substituted for
names, and a note of which failure classes were provoked deliberately. Numbers
stay as measured. Names never enter the file.

## Open blocker

**No Hyper-V test host has been released for this work.** Until one is, the
live acceptance criteria of #16 cannot be met, and sub-issues #17, #20 and #33
stay blocked on it. The remedy is a released standalone host, a dedicated
account in the two groups, and a synthetic Generation 2 guest, per the roles
table above.
