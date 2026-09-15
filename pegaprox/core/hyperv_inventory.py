"""The last answer a Hyper-V host gave, kept so the next question does not have to wait.

Reading a Hyper-V host is not a fast operation. One `Get-VM` over WinRM against a host
carrying a few dozen guests takes between half a minute and a minute, and the host view
asks for two of those answers — the inventory and the host facts — every time it is
opened. Switching between two hosts therefore meant looking at the previous host's rows
until the new host replied, which is both a wait and a lie.

So the read is moved off the request. What a route answers with is whatever this module
last heard from the host, together with when it heard it; the read itself runs on a
background thread and announces its result over the SSE channel the UI is already
listening on. Nothing here waits for a host, and nothing here returns a guess: an empty
cache answers "nothing known yet, a read is running", which the view renders as such.

Three rules keep this from becoming the standing load the patch exists to avoid:

  * **Only a viewer starts a read.** `request_refresh` is called from the routes the host
    view uses. The SSE broadcast loop and every generic cluster page read
    `get_vm_resources()`, which serves this cache and never triggers anything -- that loop
    runs once per second per watched manager, and a Hyper-V host in it was until now one
    full WinRM inventory per second.
  * **One read per host at a time.** A second viewer, or a second round of the same view,
    joins the read that is already running instead of starting another.
  * **A failed read is not retried immediately.** The failure is remembered with its
    message, so the view can say why the rows on screen are old, and the next automatic
    attempt waits out `_RETRY_AFTER_S`. The refresh button bypasses that; a person asking
    again is a reason, a render is not.

What is deliberately NOT served from here: anything a migration acts on. `vm_detail`, the
disk chains, the VM state read immediately before the disks are copied and the preflight
all go to the host as they always did. A cached inventory is fine for choosing a VM from a
list and wrong for deciding that its disks are safe to read (see the class docstring of
HyperVManager, which is still true about everything below this line).

The cache lives in this process only. After a restart the first viewer of a host waits for
one read again, which is the honest cost of not persisting a customer's inventory.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# How old an entry may get before opening the host view starts a read in the background.
# The rows stay on screen the whole time; this only decides when they are refreshed
# without anybody asking. Five minutes is well inside the time a migration wizard is open
# and far outside the rate at which a person switches between hosts.
_STALE_AFTER_S = float(os.environ.get('PEGAPROX_HYPERV_INVENTORY_TTL', '300'))

# How long after a failed read the next automatic attempt waits. Without it a view that
# polls while a host is down would ask an unreachable host as fast as the timeout returns.
_RETRY_AFTER_S = float(os.environ.get('PEGAPROX_HYPERV_INVENTORY_RETRY', '60'))

# host_id -> {'vms': list, 'facts': dict|None, 'properties': dict|None,
#             'fetched_at': float, 'error': dict|None, 'failed_at': float}
_cache: dict[str, dict] = {}
_lock = threading.Lock()

# host_id -> True while a read is running. Guarded by _lock.
_in_flight: dict[str, bool] = {}

# Hosts whose forced read arrived while one was already running, so it can be honoured
# when that one finishes rather than dropped. Guarded by _lock.
_pending_force: set[str] = set()

# host_id -> how often it has been forgotten. A read carries the number it started under
# and discards its result if it no longer matches: a host that was reconfigured or removed
# while it was being read must not have the old settings' answer written back over the
# new state, with a fresh timestamp on it. Guarded by _lock.
_generation: dict[str, int] = {}


def _now() -> float:
    return time.time()


def reset() -> None:
    """Forget everything. For tests, which share one process across cases."""
    with _lock:
        _cache.clear()
        _in_flight.clear()
        _pending_force.clear()
        _generation.clear()


def peek(host_id: str) -> dict | None:
    """The stored entry for a host, or None if this process never read it.

    A copy, so a caller cannot mutate what the next reader sees.
    """
    with _lock:
        entry = _cache.get(host_id)
        return dict(entry) if entry else None


def is_refreshing(host_id: str) -> bool:
    with _lock:
        return bool(_in_flight.get(host_id))


def cached_vms(host_id: str) -> list[dict]:
    """The VM list as last read, or an empty list. Never reaches the host.

    This is what the generic, cluster-shaped pages get. They ask every manager the same
    question once a second and none of them is worth a WinRM round trip.
    """
    entry = peek(host_id)
    return list(entry.get('vms') or []) if entry else []


def answer(host_id: str) -> tuple[dict, dict]:
    """The stored entry and its freshness fields, taken from ONE look at the cache.

    The two have to come from the same snapshot. Reading them separately leaves the
    per-VM authorization loop between them, which yields under gevent -- so a read that
    finishes in that gap would pair the old VM list with the new timestamp and
    `refreshing: false`. The client would then stop polling and show rows dated now that
    are not the rows from now, which is the failure this module exists to remove.
    """
    with _lock:
        entry = dict(_cache.get(host_id) or {})
        refreshing = bool(_in_flight.get(host_id))
    return entry, _describe(entry, refreshing)


def describe(host_id: str) -> dict:
    """The freshness fields alone, for callers that do not need the entry."""
    return answer(host_id)[1]


def _describe(entry: dict, refreshing: bool) -> dict:
    """The freshness fields every cached Hyper-V response carries.

    One shape, so the view does not have to learn two vocabularies for "this is what we
    know and this is how old it is".
    """
    fetched_at = entry.get('fetched_at') or 0.0
    return {
        'cached': fetched_at > 0,
        'fetched_at': fetched_at or None,
        'age_seconds': round(_now() - fetched_at, 1) if fetched_at else None,
        'refreshing': refreshing,
        # The classified failure of the LAST read, kept beside the data rather than in
        # place of it: what is on screen is still what the host said, and this says why
        # it has not moved since.
        'refresh_error': entry.get('error') or None,
    }


def invalidate(host_id: str) -> None:
    """Forget a host, and disown any read of it that is still running.

    Used when a host is reconfigured or removed. Bumping the generation is what stops a
    read that started under the old settings from writing its answer back afterwards --
    on a removed host that would resurrect a customer's inventory in a cache nothing
    empties again.
    """
    with _lock:
        _cache.pop(host_id, None)
        _pending_force.discard(host_id)
        _generation[host_id] = _generation.get(host_id, 0) + 1


def _should_refresh(entry: dict | None, force: bool) -> bool:
    if force:
        return True
    if entry is None:
        return True
    failed_at = entry.get('failed_at') or 0.0
    if failed_at and _now() - failed_at < _RETRY_AFTER_S:
        return False
    return _now() - (entry.get('fetched_at') or 0.0) >= _STALE_AFTER_S


def request_refresh(host_id: str, mgr, force: bool = False) -> bool:
    """Start a background read of this host unless one is unnecessary or already running.

    Returns whether a read is in flight when this returns — which is what the caller puts
    into its response, because it decides whether the view says "updating" or not.

    A forced request that arrives while a read is running is remembered rather than
    dropped. The running read may have enumerated the VM before the power action that
    forced it, so answering "one is already in progress" would leave the wrong state on
    screen until the entry goes stale.
    """
    with _lock:
        if _in_flight.get(host_id):
            if force:
                _pending_force.add(host_id)
            return True
        if not _should_refresh(_cache.get(host_id), force):
            return False
        _in_flight[host_id] = True
        _pending_force.discard(host_id)
        # Taken here rather than on the reading thread, so there is no window in which an
        # invalidate lands between the decision to read and the number the read carries.
        generation = _generation.get(host_id, 0)

    try:
        _spawn(host_id, mgr, generation)
    except Exception:                                            # noqa: BLE001
        # Nothing is going to clear the flag if nothing started. A host left marked
        # in-flight is never read again for the life of the process.
        _clear_in_flight(host_id)
        logger.exception('Could not start the inventory read of Hyper-V host %s', host_id)
        return False
    return True


def _spawn(host_id: str, mgr, generation: int) -> None:
    """Run the read off the request. A seam, so tests do not depend on thread timing."""
    threading.Thread(target=read_now, args=(host_id, mgr, generation), daemon=True,
                     name=f'hyperv-inventory-{host_id}').start()


def _clear_in_flight(host_id: str) -> None:
    with _lock:
        _in_flight[host_id] = False


def _classify(exc: Exception) -> dict:
    """The failure in the shape the Hyper-V routes already hand to the UI.

    Anything that is not already classified goes through the product's own classifier
    rather than being stringified here, so a transport exception reaches a response the
    same way it does from every other Hyper-V route instead of by a second path with its
    own rules.
    """
    from pegaprox.core.hyperv_errors import HyperVError
    if not isinstance(exc, HyperVError):
        exc = HyperVError.from_exception(exc)
    return exc.to_dict()


def read_now(host_id: str, mgr, generation: int | None = None) -> dict:
    """Ask the host, store what it said, and tell the clients watching it.

    Every failure is recorded rather than raised: this normally runs on a thread nobody is
    waiting on, and a host that is down has to show up as a host that is down, not as a
    traceback in a log the operator never reads. Returns the stored entry, or an empty
    dict when the host was forgotten while it was being read.

    `generation` is the number the host had when this read was decided on; leaving it out
    takes the current one, which is what a caller reading on its own behalf wants.
    """
    started = _now()
    if generation is None:
        with _lock:
            generation = _generation.get(host_id, 0)
    error = None
    vms = facts = properties = None
    entry = None
    try:
        try:
            vms = mgr.get_vms()
            facts = mgr.manager.host_facts()
            properties = mgr.property_report
        except Exception as exc:                                 # noqa: BLE001
            error = _classify(exc)
            logger.warning('Reading the inventory of Hyper-V host %s failed: %s',
                           host_id, error['kind'])
        entry = _store(host_id, generation, vms, facts, properties, error)
    finally:
        # In `finally` on purpose. gevent's Timeout and GreenletExit are BaseException
        # subclasses and this runs as a greenlet, so an `except Exception` alone would
        # leave the host marked in-flight for the life of the process -- read again
        # never, polled by every viewer every five seconds.
        _clear_in_flight(host_id)

    if entry is None:
        logger.debug('Discarded the inventory read of Hyper-V host %s: it was forgotten '
                     'while it was running', host_id)
        return {}

    logger.debug('Hyper-V inventory of %s read in %.1fs (%d VMs, error=%s)',
                 host_id, _now() - started, len(entry.get('vms') or []),
                 (error or {}).get('kind') or 'none')
    _announce(host_id, entry)
    _honour_pending_force(host_id, mgr)
    return entry


def _store(host_id, generation, vms, facts, properties, error) -> dict | None:
    """Write the result of a read, unless the host was forgotten while it ran."""
    with _lock:
        if _generation.get(host_id, 0) != generation:
            return None
        if error:
            # Everything already known stays. A host that stopped answering has not
            # stopped having the VMs it had a minute ago, and rows with a visible age
            # are worth more to whoever has to decide something than an empty table.
            entry = dict(_cache.get(host_id) or {})
            entry['error'] = error
            entry['failed_at'] = _now()
            entry.setdefault('vms', [])
            entry.setdefault('facts', None)
            entry.setdefault('properties', None)
            entry.setdefault('fetched_at', 0.0)
        else:
            entry = {'vms': vms or [], 'facts': facts, 'properties': properties,
                     'fetched_at': _now(), 'error': None}
        _cache[host_id] = entry
        return entry


def _honour_pending_force(host_id: str, mgr) -> None:
    """Run the forced read that arrived while this one was in flight, if there was one."""
    with _lock:
        wanted = host_id in _pending_force
        _pending_force.discard(host_id)
    if wanted:
        request_refresh(host_id, mgr, force=True)


def _announce(host_id: str, entry: dict) -> None:
    """Tell every client watching this host that there is something new to fetch.

    The frame carries no inventory data on purpose, not even a count. The REST routes
    filter the VM list per VM for the account that asks, and repeating that filter inside
    the broadcast loop would mean a second copy of an access-control decision that has
    been got wrong there before. A client hears that the host was read and asks the route
    it would have asked anyway -- which is now a cache hit and returns in milliseconds.
    """
    try:
        from pegaprox.utils.realtime import broadcast_sse
        broadcast_sse('hyperv_inventory', {
            'host_id': host_id,
            'fetched_at': entry.get('fetched_at') or 0.0,
            'ok': not entry.get('error'),
        }, host_id)
    except Exception:                                            # noqa: BLE001
        # A broadcast that cannot be delivered must not lose the read that produced it.
        logger.debug('Could not announce the Hyper-V inventory of %s', host_id, exc_info=True)
