---
category: Infrastructure
description: 'Proxmox VE platform gotchas: API, Packer templates, bpg/proxmox Terraform provider, KubeOne integration'
file_patterns:
    - '**/packer/**'
    - '**/*.pkr.hcl'
    - '**/proxmox/**'
last_updated: "2026-10-02"
subcategory: Virtualization
technologies:
    - proxmox
    - pve
    - packer
topic: proxmox
---
# Proxmox

Proxmox VE-specific gotchas. For hypervisor-agnostic cloud-init / cidata ISO / golden-image preparation see [`cloud-init`](./cloud-init.md).

## API & Authentication

- Packer `proxmox-iso` plugin requires the full URL with `/api2/json` (e.g. `https://host:8006/api2/json`), while direct Proxmox API calls use the bare URL (`https://host:8006`). Easy to mistake — the plugin error message doesn't point at the path.
- `PROXMOX_USER` is redundant when using API tokens: the username is derived by splitting `PROXMOX_TOKEN` on `=` to get the `user@realm!tokenid` prefix. Prompting for it separately misleads users into believing both are required.

## Packer Templates

- Our Packer templates ship as three variants: `ubuntu` (base), `ubuntu-k8s` (swap off, `br_netfilter`, `ip_forward` enabled), `rocky` (Rocky Linux 9) — built via a single `build.sh` with env-var credential sourcing.
- The build network (the dedicated build VLAN — the exact bridge is cluster-specific, so verify per cluster) has no DHCP. VM templates using `dhcp4: true` or `--bootproto=dhcp` will never get an IP, causing the Packer SSH communicator to time out after 60 minutes. Use a static build IP in autoinstall/kickstart for the build phase only; `cloud-init clean --logs` at the end of the provisioner removes it.
- Ubuntu 24.04 autoinstall (subiquity) shows a confirmation screen during boot. The Packer `boot_command` must send `yes<enter>` after a delay or the install stalls indefinitely.
- Proxmox VirtIO NICs get predictable kernel names (`ens18`, `enp6s18`) by default, which differ between VMs using the Proxmox `initialization` block vs. a cidata ISO. Force classical naming (`eth0`, `eth1`) in our templates by setting `net.ifnames=0 biosdevname=0` in both `boot_command` and GRUB (see [`cloud-init` → Network Configuration](./cloud-init.md#network-configuration)).

## bpg/proxmox Terraform Provider

### Cloud-init initialization block

- Changes to the `initialization` block after VM creation trigger a Proxmox hotplug error on ide2. Use `lifecycle { ignore_changes = [initialization] }` — cloud-init only runs on first boot anyway.
- `user_account` supports a `password` field for console/password login. Optional, set to `null` by default.
- `password` in Terraform state is acceptable for bootstrap (no vault yet). Generate via `random_password` resource.
- `user_data_file_id` and `user_account` in `initialization` are mutually exclusive. Using both causes a provider error.
- `user_data_file_id` expects a plain-text cloud-config snippet, NOT a binary ISO image. Passing an ISO as `user_data_file_id` produces garbage because Proxmox reads it as text for cicustom.
- Proxmox's own `initialization` block identifies NICs by MAC in the generated cloud-init config — that's why interface rename works reliably with the init block but not with name-based matching in cidata ISOs.
- Proxmox cloud-init `ipconfig` maps 1:1 to NICs. A second `ip_config` block without a second NIC creates config for a non-existent interface.

### File uploads (ISO, snippets)

- `proxmox_virtual_environment_file` with `content_type = "snippets"` requires SSH access to the Proxmox host. Without SSH, snippets cannot be created via the provider (feature request #2112 closed as wontfix). To avoid SSH to the node, build a cidata ISO and upload via `content_type = "iso"` — see [`cloud-init` → NoCloud datasource via cidata ISO](./cloud-init.md#nocloud-datasource-via-cidata-iso).
- `proxmox_virtual_environment_file` with `content_type = "iso"` uses API upload, not SSH — this is the reliable path when the Proxmox host doesn't grant SSH.
- `proxmox_virtual_environment_file` with `source_raw.data` is strictly text — internally uses `bytes.NewBufferString`, which corrupts binary bytes. For binary uploads (ISO images) use `source_file` with a local path; `source_raw` works only for text payloads.
- For idempotent ISO uploads, set the `checksum` field on `source_file` and embed a content hash in `file_name` (e.g. `vm1-cidata-<sha256[:8]>.iso`). Prevents unnecessary re-uploads on unchanged content and avoids overwrite conflicts when a different ISO is attached to a running VM.
- Error "failed to determine the datastore path" means the `datastore_id` is wrong (e.g. `vmstorage` which is LVM, not `dir` type). Only `dir`-type storages support snippets (typically `local`).

### cidata ISO attachment

- Mount the ISO via `cdrom { file_id = "..." interface = "ide2" }` on the VM. Skip the `initialization` block on the same VM — both use ide2 and are mutually exclusive.
- Proxmox automatically adds the `cdrom` device to `boot_order` whenever a cidata ISO is mounted, producing Terraform drift. Include `cdrom` explicitly in the configured `boot_order` — cleaner than adding `ignore_changes` for the field.
- `lifecycle { ignore_changes = [initialization, cdrom] }` needed when the same VM resource needs to support both init-block and cidata modes.
- `count` on cidata resources cannot depend on computed values (e.g. IPAM-assigned IPs). Use a separate `enable_*` boolean variable known at plan time — see also `terraform-standards → Anti-Patterns`.

## KubeOne Integration

- KubeOne API endpoint (`kubeone_api.endpoint` in Terraform output) must point to the VIP, not an individual CP node or a load balancer.
- When `ssh_private_key_file` is set in Terraform output, `ssh_agent_socket` must be `""` (not `"env:SSH_AUTH_SOCK"`), otherwise KubeOne tries the agent first and fails with "could not retrieve signers".
- For initial KubeOne bootstrap before kube-vip is running, use `ip addr add <vip>/32 dev eth0` via cloud-init `runcmd` on cp-1; this survives only until reboot so it does not conflict with kube-vip after deployment.
- `kubeone apply` hangs without `--auto-approve` when terminal detection fails (e.g. piped output or CI). Always pass `--auto-approve` in non-interactive scripts.

## Promoted (2026-08-15)

- [GOTCHA:0.85] When a second NIC is added to a Proxmox VM after initial provisioning, cloud-init does not run again, so the new interface remains unconfigured. VMs must be recreated (terraform apply -replace) so cloud-init configures both NICs at first boot.
- [DECISION:0.95] Use Ansible for node-level Proxmox operations (network, cluster join, CEPH, hardening) and Terraform only for generated secrets (API tokens) stored in state — no ansible-vault, no secrets in git.
- [DECISION:0.95] Corosync must run on dedicated physical interfaces (I350 ports), never on a VLAN over the 40G bond — VLANs share queues with CEPH bulk traffic causing latency spikes that destabilize Corosync.
- [DECISION:0.90] For Proxmox clusters with Mellanox ConnectX-4 (VPI cards) connected to an Ethernet switch, ports must be switched from IB to ETH mode via mlxconfig LINK_TYPE + reboot before use; interfaces then appear as enpXsY, not ib0/ib1.
- [DECISION:0.90] Live migration traffic gets its own VLAN on the 40G bond with type=insecure and optional bwlimit to prevent RAM transfers from starving CEPH; not routed over the 1G admin interface.
- [GOTCHA:0.90] When corosync_link1 IPs are omitted from the cluster IPSET in a Proxmox firewall template with policy_in DROP, ring1 traffic is blocked — exactly the fallback path used when link0 fails, causing a split-brain risk.
- [DECISION:0.95] Proxmox cluster pipeline connects as a named login user with password (company policy: password-only, no SSH keys for humans) and uses become/sudo; root SSH is allowed only node-internally via Match Address for pvecm and live migration.
- [DECISION:0.95] Per-node root passwords are required; a single cluster-wide pve_root_password_env variable is wrong and must be replaced with per-node credentials.
- [GOTCHA:0.90] 'AllowUsers <login-user>' plus 'PermitRootLogin no' without a Match Address exception blocks root SSH even from peer cluster nodes, breaking pvecm/live-migration which require root key-auth between nodes.
- [GOTCHA:0.85] 'pveceph install' is interactive and cannot be run unattended without special handling in Ansible.
- [GOTCHA:0.85] pmxcfs does not support atomic renames under /etc/pve; write operations must use direct file writes instead.
- [GOTCHA:0.80] The VM.Monitor privilege no longer exists in Proxmox VE 9 and must be removed from Terraform role definitions.
- [PATTERN:0.85] Store Proxmox LDAP bind passwords only in the API request body (never in argv or environment variables visible in process listings) when calling the /access/domains REST endpoint.
- [GOTCHA:0.90] Proxmox nodes cannot resolve .local DNS zones, so AD/LDAP server must be specified by IP address rather than hostname when configuring an AD realm.
- [GOTCHA:0.90] Proxmox AD realm in auth-only mode requires no bind account — PVE binds as the authenticating user with their own password, storing zero credentials in the realm config.
- [GOTCHA:0.90] In Proxmox VE, the realm comment is displayed as the label in the login screen dropdown — leave it empty to fall back to the realm ID (e.g. '<ad-domain>'), which is cleaner than embedding Ansible provenance notes in user-facing UI.
- [PATTERN:0.80] For Proxmox LDAP realm sync, use a readonly bind account, scope Object-Classes to 'person, user' and 'group', apply an AD group filter, and validate with 'pveum realm sync --dry-run' before importing; keep actual user import and role assignment manual.
- [GOTCHA:0.90] ProxPatch with an enabled service triggers automatic rolling updates including VM migration and node reboots on a production cluster — enabling it is a behavioral change that must be a conscious decision, not an incidental default.
- [GOTCHA:0.95] When building Proxmox templates with Secure Boot (ovmf), pre_enrolled_keys must be set to false; otherwise shim refuses a kernel loaded via GRUB command line and the build hangs.
- [PATTERN:0.85] Before a Packer/Proxmox build, run a pre-flight API check to verify: node exists and is online, required storages exist, network bridges and VLANs are present, target VM IDs are free, and all required ISOs are present.
- [GOTCHA:0.75] Proxmox can download ISOs directly via the download-url API with SHA256 verification on the host, avoiding local bandwidth usage.
- [PATTERN:0.80] Verify Proxmox VM templates end-to-end by cloning to a test VM with cloud-init (user, throwaway SSH key, static IP via ip=/gw=), resizing disk before clone so growpart has work to do, then asserting: CloudInit-Drive present, SSH reachable by key, IP matches expected, root-FS grown, sshd rejects password auth, build-user locked and sudoers entry removed, cloud-init status=done, machine-id regenerated, guest-agent reports IP to Proxmox.
- [PATTERN:0.88] In Proxmox VM template builds, separate the build VM-ID and IP (e.g. 9004 / `<build-ip>`) from the clone VM-ID and IP (e.g. 9900 / `<clone-ip>`) to avoid address collisions when running build and clone in parallel.
- [GOTCHA:0.95] Rocky Linux ISO downloads via download.rockylinux.org can be throttled to ~1 MB/s due to bad mirror assignment; mirror1.hs-esslingen.de consistently delivers ~8 MB/s and carries both Rocky 9 and Rocky 10.
- [PATTERN:0.90] For Rocky 10 UEFI boot in Proxmox: use bios=ovmf + machine=q35 + EFI disk; pass inst.stage2=cdrom and inst.ks=cdrom:/ks.cfg on the GRUB2 command line so both CD-ROMs are found without relying on a volume label that changes per point release.
- [PATTERN:0.85] Validate SSH config effective values via `sshd -T` inside the build rather than just writing config files; this catches include-order overrides and ensures the asserted values are the runtime values.
- [GOTCHA:0.85] Placing a VM on vmbr0 (the Proxmox admin bridge) puts it in the management trust zone, giving it firewall-permitted access to SSH (port 22) and the PVE UI (port 8006) on all cluster nodes — avoid for general workloads.
- [GOTCHA:0.95] Never run two Packer Proxmox builds in parallel — all templates share the same static build IP (`<build-ip>`), causing conflicts.
- [GOTCHA:0.95] Using --force on a Packer/Proxmox template build deletes the old template before the new one is ready, leaving no fallback during the build window.
- [GOTCHA:0.90] The build IP is hardcoded in http/user-data and http/ks.cfg rather than being a Packer variable, so switching Proxmox nodes requires manually updating those files — not just the URL.
- [PATTERN:0.80] Before deploying a VM on an untested fabric VLAN, provision a throwaway Alpine LXC probe on that VLAN with a temporary IP, ping it from the management cluster, then destroy it — validates L2 trunking without committing real infrastructure.
- [DECISION:0.90] VM-level backups run exclusively via Proxmox Backup Server (PBS) at the hypervisor level; no application-level backup timers or S3 uploads exist in the VM deployment path.
- [CONVENTION:0.90] Proxmox API tokens are passed exclusively via 'op run' into the process environment; the token value is never written to disk, logs, or shell history.
- [DECISION:0.95] Embedding VM uses cpu_type=host on Proxmox to expose AVX-512 to the guest; management-cluster worker VMs mask AVX-512 and deliver only ~10 tok/s vs 52 tok/s with host passthrough.
- [DECISION:0.90] Rocky Linux clones require CPU type 'host' in Proxmox; Ubuntu 26.04 clones (template 9003) do not have this constraint.
- [PATTERN:0.85] Corosync rings can share existing host addresses across different switches (Admin, CEPH VLAN, Backup) instead of requiring dedicated hardware or subnets, reducing infrastructure cost.
- [PREFERENCE:0.80] LDAPS (port 636) is preferred over plain LDAP for Proxmox AD realm integration.
- [GOTCHA:0.90] QEMU Virtual CPU model name is misleading — check actual CPU flags (AVX, AVX2, FMA present indicates x86-64-v3 feature level); llama.cpp will use AVX2 automatically.
- [GOTCHA:0.95] PegaProx requires the `X-Requested-With: XMLHttpRequest` header on all write API requests; omitting it returns 403 CSRF rejection.
- [DECISION:0.90] PegaProx is authenticated via session login (username+password) rather than API token because PegaProx allows only one active token per user, and storing the token in 1Password would expose it via `op item edit` argv.
- [DECISION:0.85] PegaProx cluster registration uses Proxmox root password (not API token) because the PegaProx API requires `pass` or `ssh_key` at cluster creation time and does not accept token-only auth for its own connection test.
- [PATTERN:0.85] Ansible `pegaprox_clusters` role is idempotent: it diffs the desired cluster list in `group_vars/all.yml` against `GET /api/clusters`, adds missing clusters, and only reports surplus ones — never deletes automatically.

## CEPH via pveceph (verified on PVE 9.2.2 / Squid, 2026-08-22)

- [GOTCHA:0.95] `pveceph install` defaults to `--version tentacle` (Ceph 20) on PVE 9.2.x. A node shipping Squid client packages (`ceph-common` 19.2.3) silently gets a major-version mix unless `--version squid` is passed explicitly. Always pin the version to what the node's client packages already are.
- [GOTCHA:0.95] `lsblk -dn -o FSTYPE,PARTTYPE <dev>` reports EMPTY for a disk carrying a GPT with partitions — `-d` suppresses partitions, and the whole-device row has no FSTYPE of its own. Any "is this disk empty?" guard built on it passes for a partitioned disk, so the zap is skipped and `pveceph osd create` then fails with "device already in use". Use `lsblk -n` (no `-d`) plus `blkid -p -o value -s PTTYPE <dev>`.
- [GOTCHA:0.90] `lsblk -Pno` (key="value") instead of the plain column format: with an empty MOUNTPOINT the column output silently shifts the following column one field left, so `sda vfat` parses as "mounted on vfat". Any awk/cut over `lsblk -no` with optionally-empty columns is wrong.
- [GOTCHA:0.90] lsblk's `279.5G` is GiB, `lsblk -bdn -o SIZE` returns bytes. A 279.5 GiB disk is 300.1 GB decimal — a size plausibility check comparing the two units off by 1024³/1000³ rejects the correct disk.
- [GOTCHA:0.92] On a single node the stock `replicated_rule` (failure domain `host`) leaves every PG `undersized` forever, regardless of OSD count. Needs `ceph osd crush rule create-replicated <name> default osd <class>`. The rule can only be created AFTER OSDs of that device class exist.
- [GOTCHA:0.92] `pveceph fs create` builds `<fs>_data` / `<fs>_metadata` itself and accepts no `crush_rule` argument — the pools inherit the cluster default. Set `ceph config set global osd_pool_default_crush_rule <id>` before creating any pool, otherwise the RBD pool looks healthy while CephFS stays degraded next to it.
- [PATTERN:0.88] `pveceph` needs no cluster: there is no corosync/quorum check anywhere in `PVE/CLI/pveceph.pm` or `PVE/API2/Ceph/*`, so a standalone node can run init/mon/mgr/osd/pool normally. pmxcfs is quorate in local mode.
- [PATTERN:0.85] ifupdown2 (>= 3.x, `addons/link.py`) supports `link-type dummy` natively — it creates the device via netlink and the kernel loads the module through its `rtnl-link-dummy` alias. No `pre-up ip link add` and no `modules-load.d` entry needed for a dummy interface carrying an isolated CEPH network.
- [GOTCHA:0.85] `apt-get update` exits **100** when any source is refused (not 0). A verification task registering its output must use `failed_when: false`, or it aborts before an assertion can report which source failed.
- [PATTERN:0.85] `pveceph install` wraps apt-get WITHOUT `-y`; on a tty-less run it prompts and aborts. Feed `echo y |` — not `yes |`, which dies of SIGPIPE (rc 141) under `set -o pipefail` once pveceph exits, failing the task despite a successful install.
- [GOTCHA:0.80] `pveceph mon ls` does not exist (prints usage, rc != 0). Guard monitor creation with `creates: /var/lib/ceph/mon/ceph-<nodename>`; re-running without it fails with "monitor address already in use".

### Wiping disks for OSD reuse

- [GOTCHA:0.95] `ceph-volume lvm zap --destroy /dev/sdX` **refuses a whole device that still has partitions** — `RuntimeError: Device /dev/sdX has partitions.` It accepts a partition, or a partition-free device. A role that calls it on a partitioned disk fails at exactly the point where it was supposed to clear the way.
- [PATTERN:0.92] Working order to hand a partitioned disk to `pveceph osd create` (verified on PVE 9.2.2, HGST SAS via LSI SAS3008, 2026-08-22):
  1. `wipefs --all --force` on every **partition** (`lsblk -lno PATH $dev | tail -n +2`) — filesystem signatures live there, and wiping the whole device does not touch them
  2. `sgdisk --zap-all $dev` — removes GPT and the protective MBR
  3. `wipefs --all --force $dev`, then `partprobe $dev; udevadm settle`
  4. `ceph-volume lvm zap --destroy $dev` — now partition-free, this clears LVM/BlueStore remains and zeroes the first 10 MiB

  The order is mandatory: step 4 cannot run before step 2, and step 2 alone leaves the filesystem signatures that step 1 removes.

### CEPH reboot behaviour (single node, verified 2026-08-22)

- [GOTCHA:0.92] After a reboot the MDS enters `up:reconnect` and waits out `mds_reconnect_timeout` (45 s default) for clients of the previous session before going `active`. Until then `ceph health` reports `FS_DEGRADED`, `pvesm status` shows the CephFS storage as `inactive`, and mounting fails with `mount error: Job failed. See "journalctl -xe"`. PVE retries on its own and mounts as soon as the MDS is active — total delay 1–2 minutes. Not a fault; do not judge a CEPH node in the first two minutes after boot.
- [PATTERN:0.90] OSDs come back via the `ceph-volume@lvm-<id>-<uuid>.service` units symlinked into `multi-user.target.wants/`. `systemctl is-enabled ceph-osd@N` reporting `enabled-runtime` is normal and correct for LVM OSDs — the ceph-volume unit activates them at boot, which then starts `ceph-osd@N`. Checking only `ceph-osd@N` misreads the boot chain.
- [PATTERN:0.88] A dummy interface configured in `/etc/network/interfaces.d/` with `auto <name>` + `link-type dummy` survives a reboot without any modules-load.d entry — the kernel loads the module through its `rtnl-link-dummy` alias when ifupdown2 creates the device.

### Identifying boot disks before a destructive wipe

- [GOTCHA:0.95] Do NOT identify boot disks by filesystem signature (`LVM2_member`, `crypto_LUKS`, …): **Ceph OSDs are themselves `LVM2_member`**, so every OSD disk is classified as a boot disk once created, and no re-run can ever touch them. Worse, whole-device signatures have an empty `PKNAME`, so a `print $PKNAME` pipeline emits blank lines that make an "at least one boot disk found" guard pass while identifying nothing.
- [PATTERN:0.92] Identify by actual use instead: union of `findmnt -rno SOURCE | grep ^/dev/`, `zpool status -PL` device paths (a ZFS root never appears as a mounted block device), and `swapon --show=NAME`, each resolved to its parent disk via `lsblk -no PKNAME` with a `lsblk -dno KNAME` fallback. On a ZFS-root PVE node this correctly yields only the boot SSD.
- [GOTCHA:0.90] An fstab safety check must match `UUID=` / `PARTUUID=` / `by-id` references, not just the kernel name — Debian/PVE fstab conventionally uses the former, so `grep /dev/sdb /etc/fstab` clears a disk that is mounted at boot but not mounted right now (`noauto`, or a mount that failed last boot).
- [GOTCHA:0.85] Jinja's `default('x')` only replaces **undefined** values, not empty strings. Diagnostics built on `cmd.stdout | trim | default('none')` render as blanks exactly on the failure path where they matter. Use `default('none', true)`.
- [GOTCHA:0.85] Ansible tag-filters `meta: flush_handlers` like any other task. An untagged one inside a role whose tasks all carry a tag is skipped under `--tags <that tag>`, so the handler runs only at end of play — after any assertion that depends on it having run.

## RADOS Gateway (S3) on a pveceph node (2026-08-23)

Verified on PVE 9.2.2 / Debian 13 / Ceph Squid 19.2.5, single node.

**`pveceph` has no RGW support.** No `pveceph rgw create`, and PVE deploys Ceph
without an orchestrator, so `ceph orch apply rgw` is unavailable too. The
gateway has to be assembled by hand: `radosgw` package, `ceph auth get-or-create
client.rgw.<inst>`, pools, a `[client.rgw.<inst>]` section in
`/etc/pve/ceph.conf`, and the packaged unit `ceph-radosgw@rgw.<inst>`.

**Keyring belongs in `/etc/ceph/`, not `/etc/pve/priv/`.** Several guides
recommend the latter for cluster-wide distribution. `/etc/pve/priv` is
`drwx------ root:www-data`; the daemon runs as `ceph` (`--setuser ceph`) and
gets `Permission denied`. On a single node the shared location buys nothing.

**`ceph osd pool create .rgw.root 8` fails with exit code 0.** Ceph refuses any
pool name starting with a dot ("pool names beginning with . are not allowed")
and reports it only on stderr — a playbook checking the return code believes it
succeeded and fails much later with "unrecognized pool". Let RGW create that
pool itself through the internal path, like Ceph does for `.mgr`, then apply
CRUSH rule and replication to it afterwards.

**The pg_autoscaler floor is `pg_num_min` = 32.** Declaring a smaller `pg_num`
at pool creation is decoration — it is raised again immediately.

**Bind RGW to `127.0.0.1` and terminate TLS in front of it.** A daemon bound to
a VLAN address fails to start whenever that interface comes up after the
service, which on a reboot it sometimes does. Loopback always exists.

**A proxy in front of RGW must forward `$http_host`, not `$host`.** `$host`
strips the port, so a client addressing `https://name:443/` signs `name:443`
while RGW receives `name` → `SignatureDoesNotMatch`. Anonymous requests keep
working, which makes it look like a credentials problem.

### PVE network API

`pvesh create /nodes/<node>/network` accepts **no** `bridge_stp` / `bridge_fd`
options — PVE writes `bridge-stp off` / `bridge-fd 0` itself. Passing them
gives `Unknown option: bridge_stp` / `400 unable to parse option`.

**A bridge defined in `/etc/network/interfaces.d/` is invisible to PVE.** It
works at the kernel level but never appears in the GUI and cannot be selected
as a VM NIC — PVE reads its network model only from `/etc/network/interfaces`.
For anything guests should use, create it through the API and apply with
`pvesh set /nodes/<node>/network`. PVE stages changes in
`/etc/network/interfaces.new`; keying the apply on that file's existence also
repairs a run interrupted between create and apply.

## Never install or run anything on a Proxmox node on your own authority

A PVE node is infrastructure, not an application host, and it is not a test machine
either. **Do not install packages, deploy code, start services, open ports or create
files on a Proxmox node without being asked to, and asking means a specific yes to
that specific action.** "It is only the test cluster", "it is only temporary" and
"it is only for this measurement" are not exceptions — the node is the thing every
VM on it depends on, and it is the one machine where being wrong is expensive.

What this rules out, concretely:

- `apt install <anything>` on the node. Two independent reasons, either one enough:
  apt on PVE resolves conflicts by proposing to remove `proxmox-ve` (see the
  `qemu-utils` entry below), and even a clean install changes a machine nobody
  agreed to change.
- Running an application on the node — a venv, a Flask app, a helper daemon. It
  listens somewhere, usually on every interface rather than loopback, under an
  account that on a PVE node typically has passwordless `sudo ALL`.
- Writing working files, ISOs, checkouts or scratch directories into `/root`,
  `/mnt` or a user's home.

Where the work belongs instead: a VM on the node, a container, or the workstation.
Anything that genuinely has to touch the node is proposed first, in one message,
with the exact commands, and waits.

The same applies to stopping. A service started on a node — even one you started
yourself — is not stopped, restarted or removed because it now looks like a
mistake. Say what is there and let the owner decide; an unannounced `kill` on a
node is the same class of action as an unannounced install.

Not hypothetical — 2026-09-14, in this project: a PegaProx instance was
deployed into a user's home on a live Proxmox node to have something to click
through, along with `python3-hivex`, `ntfs-3g`, `libhivex-bin`, `wimtools` and
`genisoimage`, a venv, a 6.5 GB ISO and a Flask listener on `*:5000`. None of it
was asked for. The instance was then also stopped unasked, in the middle of the
owner looking at it.

## Packer + Windows templates on Proxmox

Measured 2026-09-16 against Proxmox VE 9.2 with `packer-plugin-proxmox` 1.2 while building an
unattended Windows Server template. Four separate failures, none of which names its own cause:

- **`communicator = "none"` does not wait for anything.** The plugin starts the VM and goes
  straight to its shutdown step, which asks the guest to power off, waits about three minutes,
  then fails with `Error converting VM to template, could not stop: VM quit/powerdown failed -
  got timeout` and **deletes the VM** — all while the installer is still running. An unattended
  Windows install that ends in `shutdown /s` therefore never survives to become a template.
- **`boot_wait` does not help on its own.** The step that honours it is the boot-command step,
  and that step is skipped entirely when `boot_command` is empty. Express the wait as a boot
  command that only waits — `boot_command = ["<wait20m>"]` — so the install gets its time.
  Nothing is typed; the step just has to exist.
- **The install medium is left out of the boot order.** With a disk and one
  `additional_iso_files` entry, the plugin lays out `sata0` = disk, `sata1` = additional ISO,
  `sata2` = `boot_iso`, and then writes `boot: order=sata0;sata1;net0`. The guest sits at the
  BIOS screen and installs nothing. Set `boot = "order=sata2;sata0"` explicitly — and read the
  indices off a real build rather than assuming them.
- **`cd_files` uploads can be refused.** Packer posts the generated image to
  `/nodes/<node>/storage/<store>/upload`; behind a reverse proxy this can come back as
  `connection reset by peer` within a second, and the cleanup then fails too (`501 Method
  'DELETE /nodes/<node>/storage/<store>/content/' not implemented`). Build the answer file into
  an ISO with `genisoimage` over SSH and reference it as an ordinary
  `additional_iso_files { iso_file = "<store>:iso/<name>.iso" }`.

**An API token needs `SDN.Use` to attach a network card.** Since Proxmox 8 a bridge is an SDN
object, so the check runs against `/sdn/zones/localnetwork/<bridge>`. Without the privilege VM
creation fails with `403 Permission check failed (/sdn/zones/localnetwork/vmbrX, SDN.Use)`,
which reads like a node fault rather than a missing privilege.

## Windows guests: Fast Startup and hibernated volumes

- **`powercfg /hibernate on` alone does not give you Fast Startup on Windows Server.** The
  hybrid shutdown is controlled by `HiberbootEnabled` under
  `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power`, and Server leaves it off. Set
  it explicitly and end with `shutdown /s /hybrid` if you need a guest whose volume arrives
  hibernated.
- **A hibernated volume cannot be measured through a mount.** `ntfs-3g` refuses it, and the
  options that get past it (`remove_hiberfile`, `force`) change the volume — so a run that
  mounts before it measures has destroyed the state it was going to look at. `ntfsfix -n`
  reports `Windows is hibernated, refused to mount` and changes nothing; `ntfsls` lists the
  root without mounting.
- **virtio-win for out-of-support Windows:** the drivers for Server 2012 R2 are self-signed
  from release 0.1.221 on, and a self-signed boot-start driver cannot load on x64 — the boot
  manager stops at `0xc0000428` naming `viostor.sys`. **Use virtio-win 0.1.189 for Server
  2012 R2**; its `2k12R2/amd64` drivers chain to `Microsoft Code Verification Root`. 0.1.208
  is the last release carrying a cross-certificate at all, but 0.1.189 is the one to run with.
  Read the signer off the PE certificate table (data directory 4) rather than deciding from the
  Windows version — an operator who supplies an older ISO has a driver that does load.

## A Proxmox guest cannot hibernate unless you give it S4

Measured 2026-09-16 on PVE 9.2. Every VM is started with
`-global PIIX4_PM.disable_s3=1 -global PIIX4_PM.disable_s4=1` — read it off with
`qm showcmd <vmid>`. Inside such a guest, `powercfg /hibernate on` fails ("the system
firmware does not support hibernation"), no `hiberfil.sys` is ever created, and a
`shutdown /s /hybrid` silently degrades to an ordinary shutdown. Nothing reports an error
to the outside: the VM powers off exactly as it would have anyway.

Turn it on per VM with the supported `args` key, which is appended after Proxmox's own
globals so QEMU takes the later setting:

```
qm set <vmid> --args "-global PIIX4_PM.disable_s3=0 -global PIIX4_PM.disable_s4=0"
```

**Packer cannot do this.** The `proxmox-iso` builder creates the VM itself and exposes no
`args`, so a guest that must hibernate has to be prepared after the clone, not during the
build.

To run one command inside an already-installed Windows image whose autologon is spent,
register a one-shot SYSTEM service in the offline `SYSTEM` hive (`Type=0x10`, `Start=2`,
`ObjectName=LocalSystem`, `ImagePath=cmd.exe /c … & sc delete <name> & …`) in **every**
`ControlSet*`. `RunOnce` is not equivalent: it runs with the logged-in user's standard
token even for an administrator, so anything needing elevation fails there.

## Reading a hibernated Windows volume offline: order, and what refuses what

Measured 2026-09-16 against a real Windows Server 2016 volume whose `hiberfil.sys` begins
with `HIBR`:

- **`ntfsfix -n` is not a hibernation detector.** It answers `NTFS partition … was
  processed successfully` and says nothing about hibernation. The sentence
  `Windows is hibernated, refused to mount.` comes from the **mount**
  (`mount -t ntfs-3g -o rw`), which then falls back to read-only along with
  `The disk contains an unclean file system` and `Metadata kept in Windows cache`.
  To detect the state without changing anything, mount `-o ro,force` and read the first
  four bytes of `hiberfil.sys` — `HIBR` is what ntfs-3g itself decides on.
- **`ntfsfix` before the hibernation file is removed does nothing.** It refuses a
  hibernated volume, so the dirty bit survives; the following
  `-o rw,recover,remove_hiberfile` mount then drops the file but *still* falls back to
  read-only, and every write after it fails for a reason that looks unrelated. The order
  that works: mount once with `remove_hiberfile`, unmount, **then** `ntfsfix`, then mount
  read-write.
- **`hivex` refuses a dirty hive.** After a hibernation the registry hives carry unreplayed
  transaction logs, and `hivex.Hivex(path, write=True)` fails with
  `RuntimeError: Operation not supported` — on a volume that mounts read-write and passes a
  `touch` test. Clearing the hibernation file does not clear this; only Windows replaying
  its own logs on a normal boot does. Offline registry edits on a guest that hibernated are
  therefore not simply a matter of getting a writable mount.

## Running scripts in a guest through the QEMU agent (2026-09-22)

The way to act on a guest whose network is broken, e.g. straight after an import: `qm guest exec <vmid> [--timeout <s>] [--pass-stdin 1] -- <argv>` on the node, which returns JSON with `exited`, `exitcode`, `out-data`, `err-data`.

- **The agent starts a process; it does not run a shell.** Redirection, pipes and globbing need an explicit `cmd.exe /c …` or `/bin/sh -c …`.
- **Linux: send the script on stdin to `/bin/bash -s`** with `--pass-stdin 1`, which carries up to 1 MiB. Use bash, not `sh`: on Debian `sh` is dash.
- **Windows: do not pipe a script into `powershell.exe -Command -`.** It reads standard input the way an interactive console does. Measured 2026-09-22 against pwsh 7 (locally, not yet on Windows PowerShell 5.1 in a guest): the whole script went in, nothing came out, exit code 0. A run that reports nothing and succeeds is the worst possible result. Instead write the script to a file (`cmd.exe /c "more > C:\Windows\Temp\x.ps1"` with the text on stdin; `more` copies stdin to the file), run it with `powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File <path>`, then delete it. The agent runs as SYSTEM, so `C:\Windows\Temp` is writable.
- **`exited: 0` means the timeout expired while the command was still running.** Its output is lost, but whatever it did so far is done. Raise `--timeout` and inspect the guest; never re-run a script that changes things, because it would start the same work a second time. (Taken from `qm guest exec`'s status semantics; not yet seen on a production cluster.)
- Judge a run by all of these: `exited == 1`, `exitcode == 0`, and a closing line the script prints as its last act (e.g. `INFO script=<name> done`). Without that closing line, a script that was cut short looks the same as one that had nothing to report.

## VM firewall via bpg/proxmox (verified 2026-09-30, PVE 9.2.11, bpg 0.114)

- **VM rules do nothing while the datacenter firewall is off.** `pve-firewall` compiles no rules at all when `cluster.fw` has `enable: 0` (`src/PVE/Firewall.pm`: `return … if !$cluster_conf->{options}->{enable}`). VM options `enabled = true` plus NIC `firewall=1` is not enough.
- **Enabling the datacenter firewall with the default `policy_in DROP` locks out every management client outside the node's own subnet** (8006, 22 and ping only from `local_network`). This happened on a test VM. Safe order: `pvesh set /cluster/firewall/options -policy_in ACCEPT -policy_out ACCEPT -enable 1`. With `ACCEPT` the host and all VMs with `firewall=0` behave as before. Recovery: SSH via a VM in the same subnet (allowed by `local_network`), or the console, then `pvesh set /cluster/firewall/options -enable 0`.
- `ipfilter = true` on a VM means an empty `ipfilter-netX` set unless you create one: define `proxmox_virtual_environment_firewall_ipset` named `ipfilter-net0`/`ipfilter-net1` with the VM's addresses (containers get theirs implicitly, VMs do not). Verified: a secondary address on the NIC gets no answer.
- `proxmox_virtual_environment_firewall_rules` referencing a VM ipset (`dest = "+name"`) must `depends_on` the ipset resource. Otherwise the parallel create fails (`no such ipset`) after some rules are already written, and every retry aborts with `Existing rules detected`. Delete the orphaned rules (`DELETE …/firewall/rules/<pos>`, highest pos first) before re-applying.
- A bridge defined in `/etc/network/interfaces.d/` (here `vmbr9`, not visible in the PVE network API) works as a VM NIC bridge through the bpg provider: VM creation and start succeed.


