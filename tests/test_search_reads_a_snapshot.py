# What the global search is allowed to cost.
#
# These endpoints used to ask every connected cluster for a fresh `/cluster/resources`
# walk, one after the other, on every request — with `max_age=0`, which means "no cache,
# go and ask". That is the heaviest question PegaProx puts to a Proxmox cluster, the
# broadcast loop already asks it once a second for every watched cluster, and the search
# was asking it again for a keystroke. With several clusters registered, or one that is
# slow to answer, a search takes as long as the sum of them and each can wait out its own
# ten-second timeout.
#
# A search is a lookup, not a live view: a name, a VMID, a node and a tag do not move
# inside the window these routes now accept. What the tests below guard is that they ask
# for a snapshot and that a Hyper-V source is never asked at all.

from pegaprox.api import search as search_api


_VMS = [
    {'vmid': 100, 'name': 'api-db01', 'node': 'n1', 'type': 'qemu', 'status': 'running',
     'cpu': 0.5, 'mem': 120, 'maxmem': 200, 'maxdisk': 10, 'tags': 'web'},
]


def _fake_mgr(api, cluster_id='cluster_1', cluster_type='proxmox', vms=_VMS):
    m = api.make_fake_manager(cluster_id=cluster_id, cluster_type=cluster_type,
                              get_vm_resources=list(vms))
    m.is_connected = True
    m.config.name = 'Cluster One'
    m.nodes = {}
    return m


def _max_ages(mgr):
    """Every `max_age` the route asked this manager for, however it was passed."""
    seen = []
    for call in mgr.get_vm_resources.call_args_list:
        seen.append(call.kwargs.get('max_age', call.args[0] if call.args else 0))
    return seen


def test_the_search_asks_for_a_snapshot_not_a_fresh_walk(api, seed):
    admin = seed.user('root', role='admin')
    mgr = _fake_mgr(api)
    api.set_manager('cluster_1', mgr)

    resp = api.as_user(admin).get('/api/global/search?q=api&type=all')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert mgr.get_vm_resources.called, 'the search never read the cluster at all'
    assert all(age > 0 for age in _max_ages(mgr)), (
        'the search asked for a live cluster walk; with max_age=0 every request pays for '
        f'one per cluster (asked for: {_max_ages(mgr)})')


def test_the_summary_asks_for_a_snapshot_too(api, seed):
    admin = seed.user('root', role='admin')
    mgr = _fake_mgr(api)
    mgr.get_nodes.return_value = []
    mgr.get_node_status.return_value = {}
    api.set_manager('cluster_1', mgr)

    resp = api.as_user(admin).get('/api/global/summary')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert all(age > 0 for age in _max_ages(mgr)), _max_ages(mgr)


def test_the_window_is_named_and_not_a_number_in_four_places():
    """One constant, so the four reads cannot drift apart from each other."""
    assert search_api.SEARCH_MAX_AGE_S > 0
    source = open(search_api.__file__).read()
    assert 'get_vm_resources()' not in source, (
        'a route is still asking for a live walk with no max_age')
    assert source.count('max_age=SEARCH_MAX_AGE_S') == 4


def test_a_hyperv_source_is_never_asked_for_its_vms_by_a_search(api, seed, monkeypatch):
    """A search must not put a question to a Hyper-V host, directly or indirectly.

    `Get-VM` over WinRM takes tens of seconds on a host carrying a few dozen guests. The
    manager answers from `hyperv_inventory`, which is filled when the host connects and
    whenever something else reads it — never from here. A route that reached the host
    would make the search as slow as the slowest hypervisor in the estate.
    """
    admin = seed.user('root', role='admin')
    mgr = _fake_mgr(api, cluster_id='hv_1', cluster_type='hyperv', vms=[])
    api.set_manager('hv_1', mgr)

    refreshes = []
    monkeypatch.setattr('pegaprox.core.hyperv_inventory.request_refresh',
                        lambda host_id, m, **kw: refreshes.append(host_id))

    resp = api.as_user(admin).get('/api/global/search?q=api&type=all')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert refreshes == [], 'the search started a WinRM read of a Hyper-V host'
    assert not mgr.get_vms.called, 'the search asked a Hyper-V host to enumerate its VMs'
    assert not mgr.vm_detail.called
    assert not mgr.get_networks.called
