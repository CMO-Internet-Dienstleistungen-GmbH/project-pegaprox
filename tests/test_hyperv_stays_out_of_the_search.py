# A global search must never put a question to a Hyper-V host — fork issue #15.
#
# `Get-VM` over WinRM takes tens of seconds on a host carrying a few dozen guests, and a
# search runs on a keystroke. So the Hyper-V manager answers the generic resource question
# from `hyperv_inventory`, which is filled when the host connects and whenever something
# else reads the host, and never from here. One route that reached through would make every
# search as slow as the slowest hypervisor in the estate, and it would do so for everybody
# — including the installations that have no Hyper-V host and never asked for one.
#
# This lives apart from the search's own tests on purpose. What the search costs in general
# is a separate patch (fork issue #43); what it must not do to a Hyper-V source belongs
# here, with the patch that introduced the source.

_VMS = [
    {'vmid': 100, 'name': 'api-db01', 'node': 'n1', 'type': 'qemu', 'status': 'running',
     'cpu': 0.5, 'mem': 120, 'maxmem': 200, 'maxdisk': 10, 'tags': 'web'},
]


def _hyperv_mgr(api, cluster_id='hv_1'):
    mgr = api.make_fake_manager(cluster_id=cluster_id, cluster_type='hyperv',
                                get_vm_resources=[])
    mgr.is_connected = True
    mgr.config.name = 'Hyper-V host'
    mgr.nodes = {}
    return mgr


def _proxmox_mgr(api, cluster_id='cluster_1'):
    mgr = api.make_fake_manager(cluster_id=cluster_id, get_vm_resources=list(_VMS))
    mgr.is_connected = True
    mgr.config.name = 'Cluster One'
    mgr.nodes = {}
    return mgr


def test_a_search_never_enumerates_a_hyperv_host(api, seed, monkeypatch):
    """Not by asking it, and not by starting a read of it in the background either."""
    admin = seed.user('root', role='admin')
    mgr = _hyperv_mgr(api)
    api.set_manager('hv_1', mgr)

    refreshes = []
    monkeypatch.setattr('pegaprox.core.hyperv_inventory.request_refresh',
                        lambda host_id, manager, **kwargs: refreshes.append(host_id))

    resp = api.as_user(admin).get('/api/global/search?q=api&type=all')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert refreshes == [], 'the search started a WinRM read of a Hyper-V host'
    assert not mgr.get_vms.called, 'the search asked a Hyper-V host to enumerate its VMs'
    assert not mgr.vm_detail.called
    assert not mgr.get_networks.called


def test_a_hyperv_host_does_not_hold_up_the_clusters_beside_it(api, seed):
    """A registered Hyper-V source must not change what a search finds elsewhere."""
    admin = seed.user('root', role='admin')
    api.set_manager('hv_1', _hyperv_mgr(api))
    api.set_manager('cluster_1', _proxmox_mgr(api))

    resp = api.as_user(admin).get('/api/global/search?q=api&type=all')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    found = sorted(r.get('vmid') for r in resp.get_json()['results'] if r.get('vmid'))
    assert found == [100]


def test_the_summary_does_not_enumerate_a_hyperv_host_either(api, seed, monkeypatch):
    """The tallies read the same manager method and must stay off the host as well."""
    admin = seed.user('root', role='admin')
    mgr = _hyperv_mgr(api)
    mgr.get_nodes.return_value = []
    mgr.get_node_status.return_value = {}
    api.set_manager('hv_1', mgr)

    refreshes = []
    monkeypatch.setattr('pegaprox.core.hyperv_inventory.request_refresh',
                        lambda host_id, manager, **kwargs: refreshes.append(host_id))

    resp = api.as_user(admin).get('/api/global/summary')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert refreshes == []
    assert not mgr.get_vms.called
