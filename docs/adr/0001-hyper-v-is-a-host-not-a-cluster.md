# ADR 0001 — A Hyper-V source is a host, not a cluster

**Status:** accepted
**Date:** 2026-09-14
**Scope:** fork issue #15 (Hyper-V migration source)

## Context

The first implementation registered a Hyper-V host as `cluster_type: 'hyperv'` in the
cluster configuration. It therefore became a cluster like any Proxmox cluster: it appeared
in the cluster list, in the resource table, in the datacenter overview, and in the
cross-cluster migration dialog.

None of that is true of the hypervisor PegaProx already supports as a migration source.
An ESXi host is held as a VMware server; only a thin façade — `ESXiClusterManager`, 149
lines in `pegaprox/core/esxi_cluster.py` — is placed into `cluster_managers`, and solely so
the migration engine can find the host as a source:

```python
# pegaprox/app.py
load_vmware_servers()
for vmw_id, vmw_mgr in g.vmware_managers.items():
    if getattr(vmw_mgr, 'server_type', '') == 'esxi':
        g.cluster_managers[vmw_id] = ESXiClusterManager(vmw_id, vmw_mgr)
```

What this does **not** do is hide the host from the UI. `/api/clusters` builds its answer
from `cluster_managers.items()` (`pegaprox/api/clusters.py:82`) with no filter on
`cluster_type`, so an ESXi host appears in the cluster list like any other manager. The
distinction is narrower than it looks: a migration source is not written to the cluster
**configuration**, so it is not a cluster that survives as one, is not edited through the
cluster routes, and carries no cluster settings — but at runtime it is visible, and that is
what lets the cross-hypervisor wizard offer it as a source at all.

Registering Hyper-V as a cluster had consequences that were treated as separate bugs until
their common cause was found. Fourteen special cases had to be written into files that
have nothing to do with Hyper-V — eight in Python, six in JavaScript — each one catching a
Proxmox route that had reached a host with no Proxmox API:

```python
# pegaprox/api/vms.py
if getattr(manager, 'cluster_type', 'proxmox') == 'hyperv':
    return jsonify(manager.datacenter_status())
```

Further symptoms followed, and they need an honest split. Cluster health rendering `NaN`,
the resource table offering "create VM", a per-VM disk column reading `0 MB`, and a request
loop firing hundreds of `guest-info` calls at a host with no guest agent — **none of these
are specific to this patch.** They follow from any non-Proxmox manager being in
`cluster_managers`, so an ESXi host produces them too. They are reported upstream rather
than worked around here. What *was* specific to this patch is the fourteen special cases:
they existed because a Hyper-V host additionally sat in the cluster configuration and was
therefore reachable through routes an ESXi host never reaches.

## Decision

A Hyper-V source is modelled the way an ESXi source is modelled.

- It is **not** written into the cluster configuration and does not appear in the cluster
  list, the resource table or the datacenter overview.
- Its connection details live in a table of their own.
- A façade comparable in size and purpose to `ESXiClusterManager` is placed into
  `cluster_managers` so that the migration engine finds it as a source.
- Migration runs through the existing cross-hypervisor path. `hyperv_to_pve` is already a
  direction in `pegaprox/api/xhm.py` alongside `esxi_to_pve` and stays as it is.

### Why a separate table rather than `vmware_servers`

`load_vmware_servers()` reads `SELECT * FROM vmware_servers WHERE enabled = 1` and turns
**every** row into a pyVmomi-backed `VMwareManager`. A Hyper-V row there would be dialled
as a vSphere endpoint at startup. The schema is cut for vSphere as well — port 443,
`ssl_verify`, `server_type` in (`vcenter`, `esxi`) — while a Hyper-V source needs a WinRM
port and the mapping from Windows paths to SMB shares that the transfer reads from.

### Why no dedicated host view beyond what the migration needs

ESXi has a full server view because PegaProx carries a complete VMware integration —
power, snapshots, datastores. The visibility is a by-product of that integration, not part
of the migration path. A Hyper-V source is a source: it shows the hardware a migration has
to reproduce, the power actions a migration needs, and nothing that would imply PegaProx
manages Hyper-V.

## Consequences

- The fourteen `cluster_type == 'hyperv'` branches in unrelated files are removed. Their
  absence is the test for whether this ADR has actually been carried out.
- Everything listed above as a symptom disappears with them, because all of it followed
  from the host being in the resource table.
- A Hyper-V host cannot be reached through routes that assume a Proxmox API. That is the
  point, not a limitation.
- The cross-cluster migration dialog (`/api/cross-cluster-migrate`) stays what it is:
  Proxmox to Proxmox, over SSH tunnels and temporary API tokens. It never applied to a
  Hyper-V source and no longer offers itself for one.
