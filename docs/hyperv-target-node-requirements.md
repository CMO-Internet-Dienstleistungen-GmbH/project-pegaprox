# What a Proxmox node needs before it can receive a Hyper-V VM

The data never passes through PegaProx. The target node mounts the Hyper-V
host's file share read-only and runs `qemu-img convert` itself, so every
requirement below is a requirement **on the node**, not on the machine PegaProx
runs on. A node that is missing one of them fails at a different point in the
migration, and the list is ordered by where that happens.

The counterpart for the Hyper-V side is in `hyperv-verification.md`.

## How PegaProx reaches the node

Over SSH, with the credentials the target cluster is registered with
(`_connect_ssh` in `core/xhm.py`). A key is tried first, then a password. The
commands run as root: either the account is root, or PegaProx pipes a
base64-encoded script into `sudo bash` (`_Node._as_root` in `core/hyperv_xhm.py`),
so an unprivileged account needs password-less sudo.

Nothing in a migration reaches the node over the Proxmox API alone. A cluster
that PegaProx can talk to but not log in to over SSH will pass its connection
test and then fail at the first transfer.

## Installed by hand: `cifs-utils`

The one package nothing installs for you.

```bash
apt-get install -y cifs-utils
```

`mount -t cifs` is how the node reads the source disks. PegaProx neither probes
for it before the run nor installs it: the preflight only states it as a
condition, and the transfer reports the mount failure with the package name in
it. Without it, a migration fails at the mount, before a byte moves.

The mount itself needs no configuration. PegaProx writes a credentials file per
migration, mounts under `/mnt/pegaprox-hyperv/<migration-id>` and removes both
afterwards whether or not the copy worked. The options are fixed and not
configurable, because two of them are load-bearing:

| Option | Why |
|---|---|
| `ro` | The source is a customer's running hypervisor. This product has no business being able to write to it. |
| `vers=3.0` | The minimum every supported Windows Server speaks, and it stops the negotiation dropping to SMB1. |
| `cache=none` | `qemu-img` opens the source with `O_DIRECT`, which a CIFS mount only supports when it was mounted this way. |
| `noserverino`, `nobrl` | Keep inode numbers stable enough for a long sequential read. |

## Already on a Proxmox node

Present on any standard PVE installation, listed so a hardened or minimal node
can be checked against it:

| Tool | Comes from | Used for |
|---|---|---|
| `qemu-img` | `pve-qemu-kvm` | Converting VHDX to raw. Probed before the run. |
| `qemu-nbd` | `pve-qemu-kvm` | Attaching a file-based target volume during driver injection. Needed only for file-based storages (dir, NFS, CIFS, CephFS, GlusterFS, btrfs). |
| `losetup` | `util-linux` | Wrapping the target volume with a 512-byte sector size so its partitions appear. |

**Never install `qemu-utils` on a PVE node.** It is the package that would carry
`qemu-img` and `qemu-nbd` on plain Debian, and it conflicts with
`pve-qemu-kvm`. `apt` answers that conflict by offering to remove `proxmox-ve`.
This has been hit in practice: an injection run reported an empty `apt install
failed` message on PVE 9.2 and the node was one confirmation away from losing
`proxmox-ve`.

## Installed by PegaProx on first use

Only when the optional VirtIO driver injection actually runs, and only once per
node:

```
python3-hivex ntfs-3g libhivex-bin
```

`python3-hivex` edits the guest's registry, `ntfs-3g` provides the NTFS mount
and `ntfsfix`, and `libhivex-bin` provides `hivexsh`, which reads the guest's
Windows build number. The node needs a working `apt` for this, so an air-gapped
node has to be given the three packages beforehand.

`hivexsh` deserves its own line, because its absence does not fail: without it
the build number comes back empty, every guest falls through to the Windows 11
/ amd64 default, and the log still says the injection succeeded. Measured on a
Windows Server 2022 guest, that means the wrong driver variant is copied in.

`ceph-common` is installed on demand as well, and only when the target storage
is RBD.

### For the Linux preparation: `virt-v2v`

Only when a migration's VirtIO preparation is **Linux**, and only once per node:

```
apt-get install --no-install-recommends virt-v2v libguestfs-xfs
```

`virt-v2v-in-place` rebuilds the guest's initramfs and boot configuration inside
a libguestfs appliance, so the guest's volume groups are never activated on the
node. `libguestfs-xfs` is only a recommendation of libguestfs and is named
because RHEL-family guests put `/boot` on XFS. Recommends stay off because
`supermin` recommends a Debian kernel image.

What the install does to the node, measured on PVE 9.2 (trixie):

| | |
|---|---|
| Packages | 74–80 new, **none upgraded, none removed**. `pve-qemu-kvm` stays: it provides `qemu-system-x86` and `qemu-utils`, which libguestfs asks for without a version. |
| `mdadm` | A hard dependency of `libguestfs0t64`. Its install writes `/etc/mdadm/mdadm.conf`, **rebuilds the initramfs of the newest kernel and regenerates the grub configuration** (through `proxmox-boot-tool` where the node uses it). On a node without md arrays the file lists none and nothing else changes; check with `grep -c '^ARRAY' /etc/mdadm/mdadm.conf`. |
| Size | about 36 MB downloaded, 225 MB installed. |

Because of the `mdadm` step, a fleet is better given the packages in a
maintenance window, through configuration management, than on the first Linux
migration. PegaProx installs them only when they are missing.

The conversion runs as `LIBGUESTFS_BACKEND=direct`, since a PVE node carries
`libvirt0` but no libvirt daemon. A Ceph volume on a storage without krbd is
mapped with `rbd map` for the conversion and released again on every path.

## The VirtIO driver ISO

Injection is opt-in. A migration without it produces a VM that needs its
controller and network card set to hardware Windows already has drivers for.

**Which release is used is chosen in the wizard, per migration**, and the choice
runs in both directions:

| Guest | Release | Why |
|---|---|---|
| Windows Server 2012 R2 / Windows 8.1 (build 9600) | **0.1.189, and nothing else** | From 0.1.221 the drivers are self-signed, and a self-signed boot-start driver cannot load on x64 at all. Measured on the ISO: `viostor/2k12R2/amd64/viostor.sys` carries `Microsoft Code Verification Root` in its certificate table. |
| Anything newer | **anything except 0.1.189** | 0.1.189 contains `xp 2k3 2k8 2k8R2 w7 w8 2k12 w8.1 2k12R2 2k16 2k19 w10` — and no `2k22`, `w11` or `2k25`. A current guest given it ends up with no storage driver registered at all. |

Server and client variants of the same build are byte-identical in 0.1.189
(checked across viostor, vioscsi, NetKVM, Balloon and vioserial), so Windows 8.1
and Server 2012 R2 need not be told apart — which is fortunate, because they
share build 9600.

**The guest's version is read before anything is copied.** `Get-WindowsImage`
reads a stopped VM's VHDX in place, on the Hyper-V host, without mounting it —
about one to three seconds per disk. The build is the third component of
`Version`: `10.0.20348` is build 20348. Major and minor say nothing, because
Windows 10, Windows 11 and every Server from 2016 to 2025 all report `10.0`.

The integration services would report the same facts over KVP, but only while
the VM runs — measured: complete while running, empty three seconds after the
guest finished shutting down. A migration requires a stopped VM, so that source
is not available when it is needed.

The rule is enforced twice: in the preflight, where it is still free, and again
during the injection on the node, which refuses before it writes a single file.
The VM then stays on the hardware it was imported on and boots. The failure this
prevents is silent — Windows does not report a rejected signature, it simply
does not load the driver, and the machine stops at `0xc0000428` naming
`viostor.sys`.

**A chosen ISO is the only one used.** The injection no longer falls back to
whatever else is lying on the node when the chosen file is missing; it says so
and stops.

### Getting the ISO onto the node

The node fetches it itself. The wizard lists every ISO the node has, followed by
one entry per release it could download — `Download virtio-win-0.1.189.iso…`.
Choosing such an entry offers a storage and a button; PegaProx then asks
Proxmox' own `download-url` API to pull the file onto that storage. It never
passes through PegaProx or the browser, which is what makes it workable for a
~500 MB ISO on a cluster that is nowhere near the operator.

The storage defaults to **auto**, which means the one that already holds the
most ISOs — where an operator keeps them. It stays a field, because a node with
two ISO storages is a choice somebody may want to make.

Requirements for that to work:

- **An active storage with `iso` content** on the target node. Any one `pvesm`
  knows; the first is offered.
- **The node reaches `fedorapeople.org` on TCP 443.** Certificates are verified
  and that is not configurable — there is no publisher checksum for these files
  (the `CHECKSUM` beside the stable build covers the RPMs, and the archive
  directories carry none at all, checked 2026-09-16), so TLS is the only thing
  standing between the node and whatever answers that name.
- A node without internet access is given the file by hand, as before. Any ISO
  storage works, and the file name should carry the release — a file called
  `virtio-win.iso` states nothing about which release it is, and that is exactly
  the question a 2012 R2 guest turns on. Such a file is listed as
  "release not in the file name", is never preselected, and is refused for a
  guest that has a release requirement.

## What the preflight reads from the source before anything is copied

Every check here exists because the condition it finds turns into a migration
that fails **after** the disks have been converted. All of them run on the
Hyper-V host, read-only, against a stopped VM.

| Check | How | What it prevents |
|---|---|---|
| Guest's Windows version | `Get-WindowsImage` on the VHDX, no mount, 1–3 s per disk | The wrong driver release, which Windows does not report — it just does not load the driver |
| Which disk carries Windows | the same call, per disk | A boot entry pointing at a data disk |
| Registry opens | `reg load` on the guest's SOFTWARE hive, read-only | The driver injection failing on a hive it cannot open, after the copy — and it answers with the guest's real product name and patch level |
| Disk attached elsewhere | `Get-VHD`'s `Attached` | Copying a disk something else is writing to |
| **Hibernation / Fast Startup** | read-only `Mount-VHD`, then `hiberfil.sys` | A saved session that cannot be resumed on different hardware, discarded without anybody being told |
| File system clean | `fsutil dirty query`, **exit code** not text | An unclean volume carried onto the target, and hivex refusing its transaction logs |
| Disks released again | `Attached` after the inspection | A source VM that can no longer start — which would also end the rollback |

The inspection mounts each disk read-only and unmounts it again, about eight
seconds for a VM with one disk and three volumes. No drive letters are assigned:
the volumes are reached through their own GUID paths (`\\?\Volume{…}`), which
reads the same files and changes nothing on the host. The unmount runs in a
`finally` on every path, and the result reports `Attached` afterwards — a disk
left attached is a VM that cannot start, and the preflight blocks on it rather
than letting it pass unnoticed.

The registry is **opened**, not inferred from. An earlier version of this check read an
empty `EditionId` from `Get-WindowsImage` as "the hive is unreadable" and warned that the
injection would fail. Measured on exactly such a disk, that was wrong: `reg load`
succeeded and every value was there, including the edition DISM had not reported — and
with a newer patch level than DISM claimed (`UBR 2582` against `SPBuild 2340`). A hive
that Windows' own offline loader refuses is the only case that says anything about hivex,
which is stricter still.

`fsutil`'s answer is read from its exit code because the sentence is localised:
a German host says "ist NICHT fehlerhaft", and a comparison against English text
would read every one of them as clean.

## Network

- **The node reaches the Hyper-V host on TCP 445.** This is the connection that
  carries the disks. PegaProx's own WinRM connection to the host says nothing
  about it: they are different machines, different ports and usually different
  firewall rules.
- **The node's own SSH port from PegaProx**, port 22 unless the cluster is
  registered otherwise.
- **`fedorapeople.org` on TCP 443**, only if the node is to fetch driver ISOs
  itself. Nothing else in a migration needs outbound internet access.

## Space

A VMID is only free when the target storage holds no disks under it either.
`cluster/nextid` reads VM configs, so a number whose guest is gone but whose
volumes are still there reads as free — and allocating into it fails at
`rbd create: File exists` after the conversion has already run, or, on a storage
that would allow it, writes into a volume somebody is keeping on purpose. The
import checks the storage contents as well and skips such a number; a VMID typed
into the wizard is refused rather than quietly replaced.

The target volume is allocated with `pvesm alloc` at the disk's **provisioned**
size, not the used size, and written as raw. A thin-provisioned VHDX therefore
needs its full virtual size on the target storage unless the storage itself
does the thin provisioning. The preflight checks the free space and blocks when
it does not fit.

## Known gap

Two comments in `core/v2p.py` name `kpartx` as a required tool. It is not: the
code uses `losetup -b 512 -fP`, which creates the partition devices itself, and
`kpartx` is never invoked. The comments are stale, not the requirement.
