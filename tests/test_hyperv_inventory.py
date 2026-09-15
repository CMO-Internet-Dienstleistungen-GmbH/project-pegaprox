# The cached Hyper-V inventory — fork issue #15.
#
# What these guard is a trade, and both halves of it have to hold. Reading a Hyper-V host
# over WinRM takes tens of seconds, so no request may do it; and because no request does
# it, what a request answers with is old, so it has to say how old and it may never be
# fetched by something that merely renders.
#
# The read is stubbed out of every case by default. Under gevent a patched Thread.start()
# waits on the started event, so a mocked read would finish inside the call that spawned
# it and the asynchronous contract would never actually be exercised.

from unittest.mock import MagicMock

import pytest

from pegaprox.core import hyperv_inventory
from pegaprox.core.hyperv_errors import HyperVError, KIND_TIMEOUT, KIND_UNREACHABLE


HOST = 'hv_1'

_VMS = [
    {'vmid': 100, 'name': 'guest-a', 'status': 'stopped', 'type': 'qemu',
     'hyperv_guid': '11111111-1111-1111-1111-111111111111', 'generation': 2},
    {'vmid': 101, 'name': 'guest-b', 'status': 'running', 'type': 'qemu',
     'hyperv_guid': '22222222-2222-2222-2222-222222222222', 'generation': 2},
]
_FACTS = {'os_caption': 'Windows Server 2022', 'powershell_version': '5.1.20348.2849'}


def _manager(vms=None):
    """A Hyper-V manager that answers instantly, so timing never decides a case."""
    mgr = MagicMock()
    mgr.id = HOST
    mgr.get_vms.return_value = list(_VMS if vms is None else vms)
    mgr.manager.host_facts.return_value = dict(_FACTS)
    mgr.property_report = {'complete': True, 'missing': []}
    return mgr


@pytest.fixture(autouse=True)
def announced(monkeypatch):
    """An empty cache, and every SSE frame the reads produce, as (type, data, cluster).

    `_announce` imports `broadcast_sse` when it runs, so replacing it on the realtime
    module is what the module under test actually calls.
    """
    hyperv_inventory.reset()
    frames = []
    monkeypatch.setattr(
        'pegaprox.utils.realtime.broadcast_sse',
        lambda update_type, data, cluster_id=None, **kw: frames.append(
            (update_type, data, cluster_id)))
    yield frames
    hyperv_inventory.reset()


@pytest.fixture
def spawned(monkeypatch):
    """Records every background read that would have been started."""
    calls = []
    monkeypatch.setattr(hyperv_inventory, '_spawn',
                        lambda host_id, mgr, generation: calls.append(host_id))
    return calls


# ===========================================================================
# Serving what is known
# ===========================================================================

def test_a_host_never_read_serves_nothing_and_asks_for_a_read(spawned):
    """The first view of a host renders immediately; it does not wait for WinRM.

    An empty list here is not "this host has no VMs" — `describe` says `cached: False`,
    and that is what the view renders as "reading the host".
    """
    assert hyperv_inventory.request_refresh(HOST, _manager()) is True
    assert spawned == [HOST]
    assert hyperv_inventory.cached_vms(HOST) == []
    assert hyperv_inventory.describe(HOST)['cached'] is False


def test_a_read_fills_the_cache_and_the_next_view_costs_nothing(spawned):
    mgr = _manager()
    hyperv_inventory.read_now(HOST, mgr)
    spawned.clear()

    assert [vm['vmid'] for vm in hyperv_inventory.cached_vms(HOST)] == [100, 101]
    # Fresh — nothing to do, and the view says so rather than showing a spinner forever.
    assert hyperv_inventory.request_refresh(HOST, mgr) is False
    assert spawned == []
    assert mgr.get_vms.call_count == 1


def test_a_stale_entry_is_refreshed_without_emptying_the_table(spawned, monkeypatch):
    """Old rows stay up while the new ones are fetched. That is the point of the cache."""
    monkeypatch.setattr(hyperv_inventory, '_STALE_AFTER_S', 0.0)
    mgr = _manager()
    hyperv_inventory.read_now(HOST, mgr)
    spawned.clear()

    assert hyperv_inventory.request_refresh(HOST, mgr) is True
    assert spawned == [HOST]
    assert len(hyperv_inventory.cached_vms(HOST)) == 2


def test_the_refresh_button_reads_again_even_when_nothing_is_stale(spawned):
    mgr = _manager()
    hyperv_inventory.read_now(HOST, mgr)
    spawned.clear()

    assert hyperv_inventory.request_refresh(HOST, mgr, force=True) is True
    assert spawned == [HOST]


def test_two_viewers_share_one_read(spawned):
    """A second tab joins the read in flight instead of starting a second one.

    Without this, opening a host in three tabs is three simultaneous WinRM inventories
    against a customer's hypervisor.
    """
    mgr = _manager()
    assert hyperv_inventory.request_refresh(HOST, mgr) is True
    assert hyperv_inventory.request_refresh(HOST, mgr) is True
    assert hyperv_inventory.request_refresh(HOST, mgr, force=True) is True
    assert spawned == [HOST]


# ===========================================================================
# When the host stops answering
# ===========================================================================

def test_a_failed_read_keeps_the_rows_and_records_why(spawned):
    mgr = _manager()
    hyperv_inventory.read_now(HOST, mgr)
    mgr.get_vms.side_effect = HyperVError('host is gone', kind=KIND_UNREACHABLE)
    hyperv_inventory.read_now(HOST, mgr)

    assert [vm['vmid'] for vm in hyperv_inventory.cached_vms(HOST)] == [100, 101]
    described = hyperv_inventory.describe(HOST)
    assert described['cached'] is True
    assert described['refresh_error']['kind'] == KIND_UNREACHABLE
    assert described['refresh_error']['message'] == 'host is gone'
    assert described['refresh_error']['remedy']


def test_a_failed_read_is_not_retried_on_the_next_render(spawned, monkeypatch):
    """A view that polls while a host is down must not ask it as fast as it times out."""
    monkeypatch.setattr(hyperv_inventory, '_STALE_AFTER_S', 0.0)
    mgr = _manager()
    mgr.get_vms.side_effect = HyperVError('timed out', kind=KIND_TIMEOUT)
    hyperv_inventory.read_now(HOST, mgr)
    spawned.clear()

    assert hyperv_inventory.request_refresh(HOST, mgr) is False
    assert spawned == []
    # A person pressing refresh is still a reason.
    assert hyperv_inventory.request_refresh(HOST, mgr, force=True) is True
    assert spawned == [HOST]


def test_the_retry_window_expires(spawned, monkeypatch):
    monkeypatch.setattr(hyperv_inventory, '_STALE_AFTER_S', 0.0)
    monkeypatch.setattr(hyperv_inventory, '_RETRY_AFTER_S', 0.0)
    mgr = _manager()
    mgr.get_vms.side_effect = HyperVError('timed out', kind=KIND_TIMEOUT)
    hyperv_inventory.read_now(HOST, mgr)
    spawned.clear()

    assert hyperv_inventory.request_refresh(HOST, mgr) is True


def test_a_read_that_fails_before_it_ever_succeeded_leaves_an_empty_list(spawned):
    """Never a None the callers would have to guard, and never invented rows."""
    mgr = _manager()
    mgr.get_vms.side_effect = HyperVError('no route to host', kind=KIND_UNREACHABLE)
    hyperv_inventory.read_now(HOST, mgr)

    assert hyperv_inventory.cached_vms(HOST) == []
    assert hyperv_inventory.describe(HOST)['cached'] is False


# ===========================================================================
# The ways a read can go wrong around the edges
# ===========================================================================

def test_a_read_killed_mid_flight_does_not_block_the_host_for_good(spawned):
    """gevent's Timeout and GreenletExit are BaseExceptions, and these run as greenlets.

    A host left marked in-flight is never read again for the life of the process, and
    every viewer polls it every five seconds forever.
    """
    mgr = _manager()
    mgr.get_vms.side_effect = BaseException('the greenlet was killed')
    hyperv_inventory.request_refresh(HOST, mgr)
    spawned.clear()

    with pytest.raises(BaseException, match='killed'):
        hyperv_inventory.read_now(HOST, mgr)

    assert hyperv_inventory.is_refreshing(HOST) is False
    assert hyperv_inventory.request_refresh(HOST, mgr) is True
    assert spawned == [HOST]


def test_a_read_that_never_started_does_not_leave_the_host_in_flight(monkeypatch):
    def _cannot_start(host_id, mgr, generation):
        raise RuntimeError('no threads left')

    monkeypatch.setattr(hyperv_inventory, '_spawn', _cannot_start)
    assert hyperv_inventory.request_refresh(HOST, _manager()) is False
    assert hyperv_inventory.is_refreshing(HOST) is False


def test_a_forced_read_during_a_running_one_is_honoured_afterwards(spawned):
    """A VM started through PegaProx must show as started.

    The read already in flight may have enumerated it before the power action, so
    answering "one is already running" would leave the wrong state on screen until the
    entry goes stale -- five minutes, by default.
    """
    mgr = _manager()
    hyperv_inventory.request_refresh(HOST, mgr)          # the long one
    spawned.clear()
    assert hyperv_inventory.request_refresh(HOST, mgr, force=True) is True
    assert spawned == []                                 # joined, not started

    hyperv_inventory.read_now(HOST, mgr)                 # the long one finishes
    assert spawned == [HOST]                             # and the forced one runs


def test_an_unforced_request_during_a_running_read_queues_nothing(spawned):
    mgr = _manager()
    hyperv_inventory.request_refresh(HOST, mgr)
    spawned.clear()
    assert hyperv_inventory.request_refresh(HOST, mgr) is True

    hyperv_inventory.read_now(HOST, mgr)
    assert spawned == []


def test_the_list_and_its_age_come_from_one_snapshot(spawned, monkeypatch):
    """The route reads both while holding neither lock in between.

    `answer` exists so a read finishing in that gap cannot pair the old rows with the new
    timestamp -- which is the stale data this module removes, reintroduced one layer up.
    """
    hyperv_inventory.read_now(HOST, _manager())
    entry, freshness = hyperv_inventory.answer(HOST)

    # Whatever happens to the cache afterwards, the pair already handed out agrees.
    hyperv_inventory.read_now(HOST, _manager(vms=[]))
    assert len(entry['vms']) == 2
    assert freshness['cached'] is True


def test_a_transport_failure_is_classified_rather_than_stringified(spawned):
    """It reaches a browser, so it goes through the product's own classifier.

    A raw pypsrp or socket exception carries the host name and the WinRM URL; every other
    Hyper-V route hands such an exception to HyperVError.from_exception, and this one has
    no business having rules of its own.
    """
    mgr = _manager()
    mgr.get_vms.side_effect = OSError('Name or service not known')
    hyperv_inventory.read_now(HOST, mgr)

    error = hyperv_inventory.describe(HOST)['refresh_error']
    assert error['kind'] == KIND_UNREACHABLE
    assert error['remedy']


# ===========================================================================
# Forgetting
# ===========================================================================

def test_reconfiguring_a_host_drops_what_the_old_settings_returned(spawned):
    hyperv_inventory.read_now(HOST, _manager())
    hyperv_inventory.invalidate(HOST)

    assert hyperv_inventory.cached_vms(HOST) == []
    assert hyperv_inventory.peek(HOST) is None


def test_a_host_forgotten_while_it_was_being_read_does_not_come_back(spawned):
    """After a host is removed, a read still running under the old settings must not
    write a customer's inventory back into a cache nothing empties again."""
    mgr = _manager()
    generation = hyperv_inventory._generation.get(HOST, 0)   # what a read starting now carries
    hyperv_inventory.request_refresh(HOST, mgr)
    hyperv_inventory.invalidate(HOST)                    # removed while the read runs

    assert hyperv_inventory.read_now(HOST, mgr, generation=generation) == {}
    assert hyperv_inventory.peek(HOST) is None
    assert hyperv_inventory.is_refreshing(HOST) is False


def test_a_reconfigured_host_does_not_inherit_the_old_settings_answer(spawned):
    hyperv_inventory.read_now(HOST, _manager())
    stale = _manager(vms=[{'vmid': 999, 'name': 'from-the-old-host'}])
    generation = hyperv_inventory._generation.get(HOST, 0)
    hyperv_inventory.request_refresh(HOST, stale, force=True)
    hyperv_inventory.invalidate(HOST)                    # credentials changed

    hyperv_inventory.read_now(HOST, stale, generation=generation)
    assert hyperv_inventory.cached_vms(HOST) == []


def test_what_is_handed_out_cannot_be_mutated_by_the_caller(spawned):
    hyperv_inventory.read_now(HOST, _manager())
    hyperv_inventory.cached_vms(HOST).append({'vmid': 999})
    hyperv_inventory.peek(HOST)['vms'] = []

    assert [vm['vmid'] for vm in hyperv_inventory.cached_vms(HOST)] == [100, 101]


# ===========================================================================
# What the clients are told
# ===========================================================================

def test_the_announcement_names_the_host_and_carries_no_vms(announced):
    """The frame is a notification, not a payload.

    The REST route filters the VM list per VM for the account that asks. Putting the list
    in the frame would mean repeating that access-control decision inside the broadcast
    loop, which is a second copy of a rule that has been got wrong there before.
    """
    hyperv_inventory.read_now(HOST, _manager())

    assert len(announced) == 1
    update_type, data, cluster_id = announced[0]
    assert update_type == 'hyperv_inventory'
    # Scoped to the host, so it reaches the clients watching it and nobody else.
    assert cluster_id == HOST
    assert data['host_id'] == HOST
    assert data['ok'] is True
    # Not even a count: the REST route decides which VMs this account may see, and that
    # decision is not repeated in the broadcast loop.
    assert set(data) == {'host_id', 'fetched_at', 'ok'}


def test_a_failed_read_is_announced_too(announced):
    """Otherwise a view waiting for the frame waits for one that never comes."""
    mgr = _manager()
    mgr.get_vms.side_effect = HyperVError('host is gone', kind=KIND_UNREACHABLE)
    hyperv_inventory.read_now(HOST, mgr)

    assert announced[0][1]['ok'] is False


def test_an_announcement_that_cannot_be_delivered_does_not_lose_the_read(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError('no clients')

    monkeypatch.setattr('pegaprox.utils.realtime.broadcast_sse', _boom)

    hyperv_inventory.read_now(HOST, _manager())
    assert len(hyperv_inventory.cached_vms(HOST)) == 2


# ===========================================================================
# The load this replaces
# ===========================================================================

def test_the_generic_resource_question_never_reaches_the_host():
    """`get_vm_resources` is asked of every watched manager once a second.

    A Hyper-V manager answering it from the host was one full WinRM inventory per second
    per source for as long as an all-access client was connected — the standing load on a
    customer's hypervisor this patch is written to avoid.
    """
    from pegaprox.core.hyperv_cluster import HyperVClusterManager

    inner = MagicMock()
    cluster = HyperVClusterManager(HOST, {'name': 'hv', 'host': 'hv.example', 'user': 'u',
                                          'pass': 'p'}, manager=inner)

    assert cluster.get_vm_resources() == []
    assert cluster.get_vm_resources(max_age=6) == []
    inner.list_vms.assert_not_called()

    hyperv_inventory.read_now(HOST, _manager())
    assert [vm['vmid'] for vm in cluster.get_vm_resources()] == [100, 101]
    inner.list_vms.assert_not_called()
