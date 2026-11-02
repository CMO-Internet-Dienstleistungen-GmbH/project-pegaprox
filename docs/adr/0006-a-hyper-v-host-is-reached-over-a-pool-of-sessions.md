# ADR 0006 — A Hyper-V host is reached over a pool of sessions

**Status:** accepted
**Date:** 2026-09-18
**Scope:** fork issue #15 (Hyper-V migration source)

## Context

Every Hyper-V host had exactly one PowerShell session, `PsrpHyperVClient`, and every call
to the host went through that session's lock. The lock is right for that session: PSRP is
a stateful conversation over one shell, and two callers interleaving on it do not fail
fast — one waits for a reply the other already took, until the read timeout of 210
seconds.

What made it expensive was that the lock was per host. Every user of a host stood in one
queue, and so did the inventory refresh and the migration runner, which go through the
same `HyperVManager`. Four preflights on four different VMs took as long as the four one
after another. A disk inspection mounts VHDX files and takes seconds per disk, so one
wizard held the host for everyone else. Waiting for the lock had no time limit.

The calls themselves became visible as tasks (`pegaprox/core/hyperv_tasks.py`), with a
*waiting* state for a call that has no session yet. That showed the queue; it did not
shorten it.

## Options

**A — a pool of N independent sessions per host.** `PooledHyperVClient` holds up to N
`PsrpHyperVClient` instances and lends each to one caller at a time. Each keeps its own
WSMan connection, its own lock and its own retry for a shell the host has closed, all
unchanged.

- Pro: nothing about the single session changes, including the retry and redaction
  that were measured against real hosts. The pool is a new class around it.
- Pro: a transport failure throws away the shell it happened on. The other sessions are
  separate connections and carry on.
- Pro: sessions are created on first need, so a host that only ever sees one caller at a
  time keeps one shell open, as before.
- Contra: up to N shells on the host per PegaProx instance, each counting against the
  account's WinRM quota (see `docs/hyperv-verification.md`).
- Contra: N handshakes instead of one when load first arrives.

**B — a new session per request.** Open a shell, run the script, close it.

- Pro: no shared state at all.
- Contra: a WSMan handshake with authentication on every call. The inventory, the
  detail view and the wizard make many small calls; paying a handshake on each is the
  cost the persistent session was introduced to avoid.
- Contra: no upper bound on concurrent shells unless one is built anyway, which is
  option A with worse latency.

Rejected.

**C — one session with `max_runspaces > 1`.** pypsrp's `RunspacePool` can open several
runspaces over one WSMan connection.

- Pro: one shell on the host, one handshake.
- Contra: several greenlets would share one WSMan connection, and pypsrp makes no
  promise that a connection can serve several callers at once. The failure that
  connection sharing risks — a caller waiting on a reply another caller consumed — is
  the 210-second hang the per-session lock exists to prevent. Proving it safe would
  mean proving it for every pypsrp release that follows.
- Contra: a transport failure takes every runspace down together, since they share
  the connection.

Rejected.

## Decision

Option A.

- **N defaults to 4 and is set per host, 1 to 8** (`max_sessions` on `hyperv_hosts`).
  Four lets a wizard, an inventory refresh and a running migration proceed side by side.
  Eight keeps the load a PegaProx instance puts on a customer's hypervisor bounded and
  stays well below WinRM's default of 30 shells per user. The server validates the
  range; the form only mirrors it.
- **Hosts registered before the setting get 4, not 1.** A single session is what
  queued every user behind every other; nothing about an existing host asked for it.
  Setting a host to 1 restores the old behaviour exactly.
- **Waiting for a session is bounded** by the connection's read timeout (210 s). A
  caller that gets no session in that time receives a classified `busy` error, 503 over
  the API, instead of waiting indefinitely. Every session being held longer than one
  call may take means they are all held by something that is itself overdue.
- **A read-only violation is refused before a session is taken**, so a script that may
  not go out does not occupy a slot while it is refused.
- **Anything that changes a VM or attaches its disks takes a lock per VM**, in
  `HyperVManager._act`. Two inspections of one VM would otherwise mount the same VHDX
  twice, and the second would see the first's attachment. Reads take no lock: running
  them side by side, on the same VM too, is what the sessions are for. The VM lock is
  taken inside the task, so waiting for it shows as *waiting*.
- **Concurrent `connect()` calls build one manager**, under a lock in
  `HyperVClusterManager`. Without it each built its own pool, and the one that lost
  was never closed.
- **A manager that is replaced is closed.** Saving the host form builds a manager to
  test the connection and then registers another; the test manager is now closed, and
  so is the manager the new one replaces. Left open, every save left up to N shells on
  the host until WinRM timed them out.

No caching was added. The inspection stays live: a cached one would show the chain from
before a checkpoint merge.

## What was measured

Nothing against a host. The tests (`tests/test_hyperv_session_pool.py`) show, against
stand-in sessions synchronised on events rather than timing: N calls run together and
call N+1 waits; no more than N sessions are ever created; a transport failure resets
only its own session; the wait ends in `busy`; two inspections of one VM never overlap
while two VMs do; concurrent connects build one manager.

**Not measured:** whether four preflights on four VMs now take about as long as the
longest one on a real host. The host itself may be the next limit — several `Mount-VHD`
at once, or the storage behind them — and only a run against a host can show that. The
shell count on the host is checked with
`Get-WSManInstance -ResourceURI shell -Enumerate` during such a run.

## Consequences

- Calls to one host no longer wait for each other up to N at a time; beyond N they wait
  visibly, and not forever.
- A host sees up to N shells from each PegaProx instance. An operator who shares the
  account with other tooling, or has lowered the WinRM quotas, sets the host lower.
- `PsrpHyperVClient` is unchanged. The pool is a new class, and the manager builds it in
  one place (`HyperVClusterManager._build_manager`).
