---
category: Infrastructure
description: 'Moving Windows guests from Hyper-V to Proxmox VE: virtio-win driver versions per guest OS, what Proxmox refuses in a VM create call, and the operating rules this migration path is built on'
file_patterns:
    - '**/hyperv_*.py'
    - '**/v2p.py'
    - '**/xhm.py'
last_updated: "2026-09-23"
subcategory: Virtualization
technologies:
    - proxmox
    - hyper-v
    - virtio-win
    - windows-server
    - pegaprox
topic: hyperv-to-proxmox-migration
---
# Hyper-V → Proxmox migration

Rules and measured facts for importing Windows guests from Hyper-V into Proxmox VE.
Written against the PegaProx CMO fork (`feat/hyperv-migration-source`), but the driver
and API facts hold anywhere.

## virtio-win: the driver ISO is chosen per guest OS, not once per estate

- **Windows Server 2012 R2 requires virtio-win 0.1.189.** Nothing newer. Later ISOs ship
  drivers whose signature chain 2012 R2 does not accept, so the guest silently refuses to
  load them and boots without storage or network driver — which looks like a broken
  conversion, not like a driver problem.
  `https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.189-1/virtio-win-0.1.189.iso`
- Accepted signers for the 0.1.189 chain: Microsoft Code Verification Root, Windows Third
  Party Component CA, Hardware Compatibility Publisher.
- **The guest version is readable offline**, before anything boots: mount the NTFS volume
  and read `SOFTWARE\Microsoft\Windows NT\CurrentVersion` with hivex →
  `ProductName` and `CurrentBuildNumber`. Build **9600** is Server 2012 R2 →
  driver subdirectory `2k12R2/amd64`.
- Build numbers overlap between server and client SKUs: **9600 is also Windows 8.1**. A
  build-number table that tests the server line first misclassifies an 8.1 client whose
  `ProductName` could not be read.
- Choosing the ISO by "first file called `virtio-win.iso` found on the node" defeats all of
  the above. The version of the ISO is a decision, and 2012 R2 is the case where the
  default is wrong.

## What Proxmox refuses when the VM is created

- **`name` is validated as a DNS name.** Letters, digits, hyphens; dot only as a label
  separator; must start and end alphanumeric. An underscore — which Hyper-V allows and
  Windows administrators use constantly — is rejected with
  `invalid format - value does not look like a valid DNS name`.
  This rejection arrives from the create call, i.e. **after** the disks have been
  converted. Validate the name during preflight or a 100 GiB copy is thrown away over one
  character.
- **MAC addresses must be colon-separated.** Hyper-V reports `00155D000001`; Proxmox
  refuses that spelling outright ("does not look like a valid unicast MAC address").
- An adapter that has never run reports an all-zero MAC. That is the absence of an
  address, not an address.
- Creating a VM is an asynchronous task that holds a lock. Posting the disk configuration
  immediately afterwards fails with `VM is locked (create)` — wait for the UPID.

## Operating rules (CMO)

- **The MAC is carried over 1:1, including when Hyper-V calls it "dynamic".** Switches,
  DHCP reservations, licence bindings and firewall rules are written against it. A new MAC
  turns a migration into a new machine for everything that identified the guest that way.
- **No silent defaults in the migration wizard.** Every value the import writes into the
  target is a field, prefilled with what the source says and visible before the run starts.
  The fields exist to *see* what will happen and to intervene on purpose — not to be
  guessed at afterwards from the result.
- **No automatic rollback after a failed migration.** What a failed run left on the target
  is removed when the person supervising the migration decides to remove it. Automatic
  cleanup can destroy something that is deliberately being kept — an orphaned disk of a
  VM that no longer exists may be retained for legal reasons.
- Consequence of both: a retry must never reuse a VMID whose volumes still exist on the
  target storage, and must never write into an existing volume. `cluster/nextid` does not
  know about orphaned volumes — it only looks at VM configs.

## What the host knows about the guest's OS, and when

Measured 2026-09-16 against a Hyper-V host running Windows Server 2022 (build 20348),
Hyper-V role, three Generation 2 VMs. Every figure below is from that run.

**The guest OS facts exist only while the VM runs.** They come from the integration
services over the KVP channel and are read through
`Msvm_ComputerSystem` → `Msvm_KvpExchangeComponent.GuestIntrinsicExchangeItems`:

| VM state | `GuestIntrinsicExchangeItems` |
|---|---|
| Off, before the VM was ever started | empty |
| Running, 25 s after `Start-VM` | complete |
| Off, 3 s after the guest finished shutting down | **empty again** |

The component itself is present in all three states — an empty item list is the signal,
not a missing association. A check that concludes "no KVP component" has asked the wrong
question (usually by filtering `Msvm_ComputerSystem` on `Caption='Virtual Machine'`, which
matches nothing on a localised host; enumerate with `Get-VM` and filter on the VM's id).

**This matters because a migration requires a stopped VM.** At the moment the wizard needs
the guest's Windows version, the host no longer knows it. The version has to have been
captured earlier — while the VM was still running — or read out of the VHDX.

### `OSBuildNumber` is not the build number

The one field named after the thing you want is wrong. On a Windows Server 2022 guest:

```
OSName=Windows Server 2022 Standard
OSVersion=10.0.20348      ← the build is here
OSBuildNumber=10011       ← not the build, and not 20348
OSMajorVersion=10  OSMinorVersion=0  ProductType=3  ProcessorArchitecture=9
```

Take the build from the third component of `OSVersion`. Reading `OSBuildNumber` instead
puts every modern Windows below any sane pre-Windows-10 threshold — a driver rule keyed on
it would have pinned Server 2022 to the legacy virtio-win release.

`ProductType`: 1 = workstation, 2 = domain controller, 3 = server. That is what separates
Windows 8.1 from Server 2012 R2, which share build 9600 and need different driver
subdirectories.

### Other fields worth knowing

- `IntegrationServicesVersion` in the KVP items (`10.0.20348`) is the guest's own report
  and is only there while it runs. `Get-VM`'s `IntegrationServicesVersion` property reads
  `0.0` for a stopped VM, so it cannot be used to tell "never had them" from "is off".
- `NetworkAddressIPv4`, `NetworkAddressIPv6`, `RDPAddressIPv4`, `RDPAddressIPv6` no longer
  carry addresses: each returns a deprecation notice pointing at
  `Msvm_GuestNetworkAdapterConfiguration`. Parsing them as addresses yields a sentence.
- `FullyQualifiedDomainName` is the guest's hostname and is genuinely useful — it is also
  the thing that makes starting the imported copy beside the original a collision.

## Reading a stopped guest's Windows version off its VHDX

`Get-WindowsImage -ImagePath <vhdx> -Index 1` reads the image in place — no mount, no
attach, nothing written — on the Hyper-V host, for a VM that is switched off. Measured
2026-09-16 on Windows Server 2022 (build 20348): 0.7 to 1.5 seconds per 100 GiB disk.

This is the answer to "the KVP items only exist while the VM runs". A migration needs the
VM off; the disk still knows.

What comes back, and what each field is worth:

| Field | Example | Use |
|---|---|---|
| `Build` | `20348` | **The one that decides things.** Its own field — no parsing of `Version` needed |
| `Version` | `10.0.20348.2582` | Build plus UBR. Display |
| `Architecture` | `9` | 0 = x86, 9 = x64, 12 = arm64. Chooses `amd64` vs `x86` in the driver path |
| `SystemRoot` | `Windows` | The directory the drivers are copied into; ntfs-3g is case-sensitive and the injection otherwise guesses `Windows`/`WINDOWS`/`windows` |
| `EditionId`, `InstallationType` | `ServerStandard`, `Server` | Come from the guest's SOFTWARE hive |
| `ImageSize` | — | The file size, not the virtual size |
| `ImageBootable` | `Unknown` | Useless in practice |

**Empty `EditionId` with a filled `Build` means the SOFTWARE hive could not be read.** That
is the same hive the driver injection edits with hivex, where it fails with "Operation not
supported" — after the disks have been converted. So the pair is worth reading for its
absence even when its contents are not needed.

Do not assume it means an unclean file system: on the disk where this was observed,
`fsutil dirty query` reported clean. The one thing that VM differed in was its partition
order (ESP first, then the Windows volume), which is the more likely reason `-Index 1`
came back partial.

## Looking inside the disk without starting the VM

`Mount-VHD -ReadOnly` plus `Dismount-VHD` in a `finally`. Eight seconds for a VM with one
disk and three volumes. Two things make it usable:

- **No drive letters are assigned.** Reach the volumes through their own GUID paths
  (`\\?\Volume{...}`, from `Get-Volume`'s `Path`), which reads the same files and changes
  nothing on the host. Assigning a letter would.
- **Report `Attached` afterwards.** A VHDX left attached is a VM that cannot start, which
  is both its own problem and the end of the rollback story for that migration.

What is worth reading there: `hiberfil.sys` and its size (hibernation or Fast Startup),
`pagefile.sys`, whether `Windows\System32\config\SOFTWARE` exists, and the file system's
dirty bit.

**`fsutil dirty query` answers in the host's language** — a German host says "ist NICHT
fehlerhaft". Read the **exit code** (0 = clean, measured), never the sentence.

**`manage-bde.exe` may simply not be installed** on a Hyper-V host (it was not on the one
measured), so it is not a dependable BitLocker source.

**A PSRP session has a reduced `PATH`.** `manage-bde` and similar are not found by name;
call external executables through `Join-Path $env:SystemRoot 'System32\...'`.

## What is inside virtio-win 0.1.189

Measured on the ISO (sha256 `97e9f9c7…85c566`, 477 MB, 2026-09-16). One directory per
driver family, below it one per Windows variant, below that the architecture:

```
viostor/  xp 2k3 2k8 2k8R2 w7 w8 2k12 w8.1 2k12R2 2k16 2k19 w10
viostor/2k12R2/amd64/  →  viostor.sys  viostor.inf  viostor.cat  viostor.pdb
```

**There is no `2k22`, no `w11` and no `2k25`.** A Server 2022 guest given this ISO ends up
with no storage driver registered at all — which is why the rule runs in both directions:
build 9600 takes 0.1.189 and nothing else, and 0.1.189 goes to build 9600 and nothing else.

**Server and client variants of the same build are byte-identical.** Every one of the 27
files under `2k12R2/amd64` matches `w8.1/amd64` across viostor, vioscsi, NetKVM, Balloon
and vioserial; the same holds for `2k19`/`w10`, `2k16`/`w10`, `2k8R2`/`w7` and `2k12`/`w8`.
So the builds that two Windows editions share (9600 = 8.1 and 2012 R2, 17763 = Win10 1809
and Server 2019) need not be told apart for the driver choice.

`viostor/2k12R2/amd64/viostor.sys` carries `Microsoft Code Verification Root` in its
certificate table — the chain an out-of-support Windows accepts, and the reason for the
whole rule.

## Get-WindowsImage's empty fields are not evidence of a broken hive

Measured 2026-09-16 on a Server 2022 disk where `Get-WindowsImage` returned `Version` and
`Architecture` but left `EditionId`, `InstallationType` and `ProductType` empty.

The obvious reading — "the SOFTWARE hive cannot be read, so hivex will refuse it too and
the driver injection will fail" — is **wrong**. On that same disk, mounted read-only:

```
reg load HKLM\Offline <volume>\Windows\System32\config\SOFTWARE   → exit 0
ProductName        Windows Server 2022 Standard
EditionID          ServerStandard      InstallationType  Server
CurrentBuildNumber 20348               UBR               2582
DisplayVersion     21H2                CurrentVersion    6.3
```

Everything is there. And the registry is the *better* source: `UBR 2582` against DISM's
`SPBuild 2340` on the same disk, so DISM was reporting an older state than the one
installed.

Two things follow:

- **Open the hive rather than inferring anything from DISM's gaps.** `reg load` +
  `Get-ItemProperty` + `[gc]::Collect()` + `reg unload`, while the disk is mounted
  read-only anyway. A hive Windows' own offline loader refuses is the only case that says
  anything about hivex, which is stricter still and edits the same file.
- **`CurrentVersion` in the registry reads `6.3` on Server 2022.** It has been frozen
  since Windows 8.1 and is useless as a version source. `CurrentBuildNumber` + `UBR` are
  the real ones; `Get-WindowsImage`'s `Version` carries both as its third and fourth
  component.

A file system check does not explain the empty fields either — that disk reported
`fsutil dirty query` exit 0, i.e. clean.

## Linux guests through `qm guest exec` (measured on CentOS 6/7/8, Ubuntu 18.04/24.04)

- **CentOS 6 cannot be reached through its agent.** qemu-ga 0.12.1 has no `guest-exec` (it arrived with 2.5), and SELinux refuses its `guest-file-write` even to `/var/tmp` (mode 1777, as root). No script can be placed and started, not even via a cron drop. Such guests are repaired on the console.
- **RHEL/CentOS 7+ ship `guest-exec` disabled**: `BLACKLIST_RPC` (7) / `FILTER_RPC_ARGS` (8) in `/etc/sysconfig/qemu-ga`. `qm guest cmd <id> info` lists every command with `enabled`; check it before running anything.
- **Everything the agent starts runs as `virt_qemu_ga_t`** on RHEL/CentOS. That domain may not execute or even stat `/usr/sbin/ip`, may not read `/sys/fs/selinux/enforce`, may not write `/etc`. A script that tests `[ -r /sys/fs/selinux/enforce ]` concludes "SELinux off" — decide from `/proc/self/attr/current` (`user:role:type:level` = SELinux) and treat an unreadable mode as enforcing.
- **`semanage permissive -a virt_qemu_ga_t` is not enough where NetworkManager runs**: the agent's requests pass, but NetworkManager's D-Bus replies are denied on its side (`NetworkManager_t -> virt_qemu_ga_t`, `send_msg`, `permissive=0`), and `nmcli` hangs without failing. `setenforce 0` for the repair works; bound every `nmcli` call with `timeout`.
- **The way back through `qm guest exec` is not UTF-8-safe**: an em dash in `out-data` arrives as `â\x80\x94`. Keep guest script output ASCII.
- **netplan `set-name` breaks Ubuntu 18.04 after a restart** once cloud-init no longer renames at boot: without the `.link` file in the initramfs the adapter boots under its kernel name (`ens18`), the generated `[Match] Name=eth0` matches nothing, the adapter stays down. Match by `macaddress` only.
- **netplan 0.99 (18.04) predates `to: default`** — write `to: 0.0.0.0/0` / `"::/0"`.
- **cloud-init with a changed datasource sees a new instance** and re-renders the network at the next boot. `network: {config: disabled}` in `/etc/cloud/cloud.cfg.d/` is honoured even for `boot-new-instance` (logged as "network config is disabled by system_cfg").
- **bash < 4.4 (CentOS 6/7)**: an empty array is unset under `set -u` — `${a[*]}` aborts; use `${a[*]-}` / `${a[@]+"${a[@]}"}`. A minimal RHEL has no `cmp` (diffutils); compare with `cksum`. systemd < 239 (18.04) has no `resolvectl`; use `systemd-resolve --status`.
- **vault.centos.org answers 403 under load**; `http://archive.kernel.org/centos-vault/` mirrors it.

## Correction (2026-09-23): enforcing SELinux on RHEL/CentOS is a hard stop, not a setenforce

An earlier note here framed `setenforce 0` as the remedy for a confined `virt_qemu_ga_t`. The maintainers' position (2026-09-23): the tool never switches SELinux off or loosens it, and never advises the operator to. If the policy denies the agent, the run stops with the exact reason and the guest keeps its SELinux state.

The reason it cannot work through the agent, at rule level (sesearch on the shipped CentOS 7/8 targeted policy, confirmed live):
- Everything `qm guest exec` starts runs as `virt_qemu_ga_t`. That domain: no `execute` of `ip` (`ip -o link` denied), `/sys/fs/selinux/enforce` unreadable, no write to `/etc`, no D-Bus to NetworkManager. It *can* read config files on CentOS 8 (boolean `virt_qemu_ga_read_nonsecurity_files=on`).
- The policy's one escape is `virt_qemu_ga_unconfined_exec_t` → auto-transition to `virt_qemu_ga_unconfined_t` (a `files_unconfined_type`). That label lives only on `/etc/qemu-ga/fsfreeze-hook.d/`, `/usr/libexec/qemu-ga/fsfreeze-hook.d/`, `/var/run/qemu-ga/fsfreeze-hook.d/`.
- The agent may only read/exec that type, not create it, and has no `relabelto`. Every dir it can write is `virt_qemu_ga_{tmp,var_run,log,data}_t`; files there keep that type and exec stays confined. The hook dirs are not agent-writable (`dir { read search open }`, no `add_name`).
- So the escape needs an *unconfined* process to place and label the hook — which the agent is not. No agent-only path exists on an enforcing guest.

Consequence: the agent transport covers Ubuntu (agent runs unconfined) and RHEL/CentOS guests whose SELinux is permissive or disabled (proven end-to-end on CentOS 8 permissive + NetworkManager, holds across reboot). An enforcing RHEL/CentOS guest is fixed on its console, or offline from the node via its disk — a different transport, not the agent.
