# ADR 0009 — The guest agent is installed during the Linux conversion

**Status:** accepted
**Date:** 2026-10-09
**Scope:** fork issue #15 (Hyper-V migration source)

## Context

A Linux guest imported from Hyper-V arrives without network. Its configuration names
the adapters it had there (`eth0` on netvsc), the VirtIO adapter comes up under another
name, and nothing applies the old addresses to it. The only way into such a guest is the
QEMU guest agent, over its virtio-serial channel: setting the addresses afterwards is done
through `guest-exec`.

Guests from Hyper-V rarely have the agent. virt-v2v notices that and schedules its
installation — at first boot, with the guest's own package manager:

```
virt-v2v-in-place: The QEMU Guest Agent will be installed for this guest at first boot.
```

It writes `/usr/lib/virt-sysprep/scripts/5000-0003-install-qga` (`apt-get update; apt-get
install qemu-guest-agent` on Ubuntu) beside a `wait-online` step that gives up after 30
seconds. At first boot the guest has no network, so the install fails. The runner,
`/usr/lib/virt-sysprep/firstboot.sh`, moves each script to `scripts-done/` *before* running
it and empties that directory afterwards, whatever the result: the attempt is never
repeated, not even on a later boot that has network. The guest ends up with no network and
no agent, and nothing outside it can change that.

A CentOS 7 guest seemed to work because its image already carried the agent: the
GenericCloud 2009 image ships `qemu-guest-agent-2.12.0-3.el7`, so virt-v2v scheduled
nothing.

## Options

1. **Ship the packages with PegaProx** and install them offline with `dpkg -i` / `rpm -i`.
   One build per distribution and release (each linked against its own glibc and glib),
   their dependencies on minimal guests, GPL binaries in a public repository, and copies
   that age without security updates.
2. **Have the node download the packages** and install them offline. No binaries in the
   repository, but a resolver per distribution on a Debian node, which has no `dnf`.
3. **Configure the network first**, so virt-v2v's firstboot install succeeds. It needs the
   addresses at conversion time and the logic of every network stack; and it still leaves
   the agent to a single unrepeated attempt.
4. **Run the guest's own package manager during the conversion.** virt-v2v accepts
   `--run-command`, and the libguestfs appliance has outgoing network.

## Decision

Option 4. The conversion runs, as its first `--run-command`, a step that installs
`qemu-guest-agent` with the guest's own package manager (apt, dnf, yum or zypper):

1. A guest that already has the agent is left alone.
2. The guest's own sources come first: they may be an internal mirror, and they are what
   the guest is maintained from.
3. Where they fail and the release has moved to an archive, the archive is tried, checked
   against the guest's own signing keys: CentOS 7 and 8 from `vault.centos.org` at the
   guest's own point release (CentOS Stream 8 from `8-stream`), Ubuntu from
   `old-releases.ubuntu.com`, Debian from `archive.debian.org`. The archive is configured
   only for that call — a temporary apt source list and list directory, or a temporary
   `.repo` file that is removed with its cached metadata — so nothing it adds stays in the
   guest.

The step always exits 0: a failing `--run-command` stops virt-v2v, and an agent that could
not be installed is no reason to leave a guest unbootable. It reports one line,
`PEGAPROX_QGA present`, `PEGAPROX_QGA installed from <where>` or `PEGAPROX_QGA failed:
<why>`, and keeps the package manager's output in
`/var/log/pegaprox-guest-agent-install.log` in the guest. A failure ends the migration
*completed with errors*, names the reason, and the VM is still started.

It runs before the existing unlock of `/etc/sysconfig/qemu-ga`, because a freshly
installed RHEL package brings `BLACKLIST_RPC` with `guest-exec` in it.

A command's output appears only in virt-v2v's debug output, so the conversion now runs
with `-v`. The debug output goes to stderr; the script keeps it in a temporary file and
passes on only the lines the log reads — virt-v2v's own messages, `semodule`'s, and the
`PEGAPROX_QGA` line — once each. The progress lines on stdout are unchanged.

virt-v2v's own firstboot install is left in place. With the agent installed it finds
nothing to do.

## Consequences

- The target node needs outgoing HTTP(S) to the guest's package sources, or to the
  archives named above, during a Linux conversion. Without it the step reports `failed`
  and the migration completes with errors.
- A guest whose sources need credentials or a proxy the appliance does not have, a RHEL
  guest without a subscription, and a release in no archive this step knows end with
  `failed`; the agent is then installed on the console.
- The package manager runs in the guest's chroot, so its scriptlets do too. Services are
  not started there ("Running in chroot, ignoring request"); the agent starts with the
  boot.

## Measured

virt-v2v-in-place 2.6.0 and libguestfs 1.54.1 on PVE 9.2. Each guest is the distributor's
cloud image with `qemu-guest-agent` removed beforehand, imported onto ZFS, converted by
the script `conversion_script()` builds, then booted **without any network adapter** and
asked through the agent (`qm agent ping`, then `qm guest exec` writing under `/etc`).

| Guest | Agent from | Conversion | Agent answered after boot |
|---|---|---|---|
| Ubuntu 18.04 | guest's sources | 120 s | 29 s |
| Ubuntu 20.04 | guest's sources | 84 s | 11 s |
| Ubuntu 22.04 | guest's sources | 128 s | 20 s |
| Ubuntu 24.04 | guest's sources | 125 s | 11 s |
| Ubuntu 22.10 | `old-releases.ubuntu.com` | 54 s | 10 s |
| Debian 10 | `archive.debian.org` | 33 s | 10 s |
| Debian 11 | `archive.debian.org` | 51 s | 11 s |
| Debian 12 | guest's sources | 55 s | 10 s |
| CentOS 7.9 | `vault.centos.org/7.9.2009` | 98 s | 39 s |
| CentOS 8.4 | `vault.centos.org/8.4.2105` | 122 s | 20 s |
| CentOS Stream 8 | `vault.centos.org/8-stream` | 84 s | 12 s |
| Rocky Linux 8.10 | guest's sources | 112 s | 13 s |
| Rocky Linux 9.8 | guest's sources | 100 s | 19 s |
| AlmaLinux 9.8 | guest's sources | 159 s | 46 s |
| openSUSE Leap 15.6 | guest's sources (zypper) | 79 s | 11 s |
| Rocky Linux 9.8, agent kept | `present` | 122 s | 19 s |
| Ubuntu 18.04, sources pointed at an unreachable host | `failed`, reason and log path reported; conversion exit 0 | 24 s | — |

- On AlmaLinux 9.8 the agent ran as `virt_qemu_ga_t` with SELinux `Enforcing`, and
  `guest-exec` wrote under `/etc`: the install, the unlock and the permissive module
  (ADR 0008) work together.
- Without network in the appliance the guest's own sources fail first; on CentOS 7 the
  guest's `mirrorlist.centos.org` does not resolve at all.
- `yum` 3 (CentOS 7) has no `--repofrompath`, hence the temporary `.repo` file.
- Four conversions started in the same second on a cold appliance cache
  (`/var/tmp/.guestfs-0` removed) all succeeded. Twice before, in runs with parallel
  disk imports, a conversion ended with exit 1 after 5 s and no line on stdout; it could
  not be reproduced. Its reason was in the debug output under a prefix the filter
  dropped, so after a failed run the last 30 debug lines (without the appliance's kernel
  log) are passed on, prefixed `V2V_DEBUG`.
- Under `-v`, the debug output of one conversion was about 200 KB.
