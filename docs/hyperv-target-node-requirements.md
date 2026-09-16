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

## The VirtIO driver ISO

Injection is opt-in. A migration without it produces a VM that needs its
controller and network card set to hardware Windows already has drivers for.

**Which release is used is chosen in the wizard, per migration.** The node
usually holds more than one ISO, and which one a guest may be given is not a
matter of taste:

| Guest | Release | Why |
|---|---|---|
| Windows Server 2012 R2, Windows 8.1 (build 9600) | **0.1.189, nothing newer** | From 0.1.221 the drivers are self-signed, and a self-signed boot-start driver cannot load on x64 at all. 0.1.189's `2k12R2/amd64` drivers chain to Microsoft Code Verification Root. |
| Everything still in support | current stable | — |

The rule is enforced, not documented at: the injection reads the guest's build
number out of its registry before it copies anything, and refuses to write
drivers from a release that build may not have (`core/hyperv_drivers.py`). The
VM then stays on the hardware it was imported on and boots; nothing on the
volume is changed. The failure this prevents is silent — Windows does not report
a rejected signature, it simply does not load the driver, and the machine stops
at `0xc0000428` naming `viostor.sys`.

### Getting the ISO onto the node

The node fetches it itself. In the wizard, a release the node does not have is
offered with a **Fetch** button next to it; PegaProx asks Proxmox' own
`download-url` API to pull it onto an ISO storage. The file never passes through
PegaProx or the browser, which is what makes it workable for a ~700 MB ISO on a
cluster that is nowhere near the operator.

Requirements for that to work:

- **An active storage with `iso` content** on the target node. Any one `pvesm`
  knows; the first is offered.
- **The node reaches `fedorapeople.org` on TCP 443.** Certificates are verified
  and that is not configurable — there is no publisher checksum for these files
  (the `CHECKSUM` beside the stable build covers the RPMs, and the archive
  directories carry none at all, checked 2026-09-16), so TLS is the only thing
  standing between the node and whatever answers that name.
- A node without internet access is given the file by hand, as before. Any ISO
  storage works and the file name has to contain `virtio-win` or `virtio_win` —
  **with the release in it**, because a file called `virtio-win.iso` states
  nothing about which release it is, and that is exactly the question a 2012 R2
  guest turns on. Such a file is offered in the list marked as unidentified and
  is refused for a guest that has a release requirement.

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
