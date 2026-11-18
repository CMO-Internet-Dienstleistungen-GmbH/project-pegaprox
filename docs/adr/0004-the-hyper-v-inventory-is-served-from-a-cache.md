# ADR 0004 — A Hyper-V host is read in the background, not inside the request

**Status:** accepted
**Date:** 2026-09-15
**Scope:** fork issue #15 (Hyper-V migration source)

## Context

`HyperVManager` was written to hold no state: *"every answer is read when it is asked for.
A cached inventory would be wrong precisely when it matters, which is in the minutes
between a person choosing a VM and the migration reading its disks."* That is correct
about the migration, and it was applied to the whole surface, which is where it stopped
being correct.

Reading an inventory takes between half a minute and a minute on a host carrying a few
dozen guests: one `Get-VM` over WinRM, plus the host facts the same view asks for. Three
things followed from doing that inside a request.

**Switching hosts showed the wrong host's VMs.** The view fetched the new host and left
the old rows up until the answer arrived. For that half minute, one host's VM names,
generations and memory figures were on screen under another host's name — and the same
defect exists product-wide in the other views, which is what made it look like normal
behaviour rather than a bug.

**A late answer could land under the wrong host.** Nothing checked, on arrival, that the
selection was still the one the request was made for. Switching A → B → A produced
whichever answer came back last.

**The generic machinery was already reading the host once a second.** `get_vm_resources()`
is the question PegaProx asks every manager in `cluster_managers`, and the SSE broadcast
loop asks it of every *watched* manager on each one-second round. `watched_clusters()`
returns `None` — meaning "poll everything" — as soon as one client has all-access, which
every admin session has. A Hyper-V source answering that question from the host was
therefore one full WinRM inventory per second per host, for as long as an administrator
had a tab open. Nothing displayed the result: the overview deliberately skips Hyper-V
sources. This is the exact standing load the patch's own comments say must not exist.

## Decision

**The Hyper-V inventory and host facts are served from a per-process cache
(`pegaprox/core/hyperv_inventory.py`). The read runs on a background thread and announces
itself over SSE.**

- `GET /api/hyperv/<id>/vms` and `GET /api/hyperv/<id>/host` answer immediately from the
  cache, and every response carries `cached`, `fetched_at`, `age_seconds`, `refreshing`
  and `refresh_error`. The view renders the age, so nothing on screen is undated.
- A read is started by those two routes, and only when the entry is older than
  `PEGAPROX_HYPERV_INVENTORY_TTL` (300 s), absent, or explicitly asked for with
  `?refresh=1` — which is the refresh button and nothing else.
- One read per host at a time. Further viewers join it. A *forced* request arriving
  while one runs is remembered and run afterwards, because the read in flight may have
  enumerated a VM before the power action that forced it.
- A failed read keeps the previous rows and stores the classified failure (`kind`,
  `remedy` — the same shape the synchronous routes return, produced by the product's own
  `HyperVError.from_exception` rather than by rules of this module's own). The next
  *automatic* attempt waits `PEGAPROX_HYPERV_INVENTORY_RETRY` (60 s); the button does not.
- A host that is forgotten while a read of it is running disowns that read: the entry
  carries a generation, and a result whose generation no longer matches is dropped
  instead of writing a removed or reconfigured host's answer back with a fresh timestamp.
- `get_vm_resources()` and `datacenter_status()` read the cache and never reach the host.
  `get_cluster_networks()` still does, deliberately: it is a page somebody opened for
  this host, and it reads the adapters of every VM, which no cache here holds.
- When a read finishes, `broadcast_sse('hyperv_inventory', …)` is sent, scoped to the
  host's id. The frontend adds the selected Hyper-V host to its SSE subscription so the
  frame is delivered to scoped clients, not only to all-access ones.
- The cross-hypervisor wizard's source-VM picker reads `/api/hyperv/<id>/vms` for a
  Hyper-V source instead of `/api/clusters/<id>/resources`. The latter is now
  cache-only, so on a host nobody has opened it would offer an empty list with no way to
  fill it.

**What is not cached is everything a migration acts on.** `vm_detail`, the disk chains,
`get_vm_state`, `disks_are_safe_to_read` and the preflight go to the host exactly as
before. The original reasoning holds for all of them: a cached answer is fine for choosing
a VM out of a list and wrong for deciding that its disks may be copied.

### Why the SSE frame carries no VMs

It would have to be filtered per VM. `GET /vms` filters its list with
`user_can_access_vm(...)` for the account asking, because reaching a host through a single
VM-ACL entry must not hand back the whole inventory. Putting the list into the frame means
repeating that decision inside `broadcast_sse`, which already carries five such filters,
every one of them added after an audit found the frame leaking what its REST twin
withheld (`resources`, `vm_config`, `tasks`, `vmware_vms`, `vmware_vm_detail`).

So the frame says *the host was read*, and nothing else — not even a VM count, because a
count is a fact about the inventory the route is about to filter. The client asks the
route it would have asked anyway, which is a cache hit and returns in milliseconds. The
cost is one extra round trip; what it buys is no second copy of an access-control rule and
a frame that stays a few hundred bytes instead of carrying a 159-VM inventory to every
subscribed client.

`broadcast_sse` does gain one branch for it: the same permission gate every other frame
family added after those audits carries, checking `hyperv.vm.view` — the permission on the
REST route the client is told to ask. Nine lines mirroring the `vmware_vms` branch beside
it, and no filtering logic, which is the part that has to stay in one place.

### Why in-process and not in the database

The inventory is a customer's, and persisting it means it survives in a file long after
the migration it was read for. A restart costs one read per host that somebody actually
opens, which is the honest price. `hyperv_vmid_map` stays the only Hyper-V state that
outlives the process, because an ACL written against VMID 104 has to mean the same VM
tomorrow — an inventory does not.

### Why the view polls as well

The SSE frame is the normal way the wait ends. The view also asks again every five seconds
*while a read is running*, for the cases where the frame cannot arrive: a subscription that
does not include this host, a proxy that drops the stream, SSE unavailable. Those polls
read the cache only — without `refresh` nothing starts a read — so a poll that runs longer
than expected costs the Hyper-V host nothing. This mirrors what `ClusterHealthBadge` does
with the pushed health rollup.

## Consequences

- Opening or switching a Hyper-V host renders at once. What is shown is either the last
  known inventory with its age, or an explicit "reading the host" — never the previous
  host's rows and never an empty table that reads as "this host has no VMs".
- The SSE broadcast loop stops reading Hyper-V hosts entirely. A source that nobody has
  opened is never contacted, and counts zero VMs on the inventory overview until it is —
  which is what "never contacted" honestly looks like.
- A VM started or shut down through PegaProx forces a read, so the list reflects it
  without anyone pressing refresh.
- The inventory can be up to five minutes old while a wizard is open. The age is on
  screen, the refresh button is next to it, and the preflight re-reads the VM from the
  host before anything is started — which is where the freshness actually has to hold.
- `web/index.html` is rebuilt from `web/src` as always; nothing here is edited in the
  bundle.
