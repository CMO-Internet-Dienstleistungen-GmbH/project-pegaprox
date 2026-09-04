# The cluster health rollup is a cluster-wide number — the same for everyone
# looking at the cluster — but it used to be computed once per request per tab,
# each time fanning out get_node_status() plus one uncached
# GET /nodes/<node>/storage per online node. Two colleagues on the same cluster
# cost twice the Proxmox calls of one, for the same figure on screen.
#
# These tests pin the property that makes that stop: N requests inside the TTL
# cost ONE computation, and a repeat with nothing changed costs a 304.

import pytest

from pegaprox.core.health import (
    compute_cluster_health,
    get_cluster_health,
    health_etag,
    invalidate_cluster_health,
)


@pytest.fixture(autouse=True)
def _clean_health_cache():
    """The rollup cache is a module global; without this a test would inherit the
    previous one's entries and the call counts below would be meaningless."""
    invalidate_cluster_health()
    yield
    invalidate_cluster_health()


def _healthy_manager(api, cluster_id='cluster_1', nodes=('pve1', 'pve2', 'pve3')):
    """A manager that answers the three calls the rollup makes."""
    node_status = {n: {'status': 'online'} for n in nodes}
    storages = [{'storage': 'local', 'active': 1, 'total': 100, 'used': 10}]
    mgr = api.make_fake_manager(
        cluster_id,
        get_node_status=node_status,
        get_storage_list=storages,
        get_replication_status=[],
    )
    mgr.is_connected = True
    return api.set_manager(cluster_id, mgr)


def test_two_requests_cost_one_computation(api, seed):
    """The whole point of #11: cost scales with clusters, not with viewers."""
    root = seed.user('root', role='admin', tenant_id='default')
    seed.db.save_cluster('cluster_1', {'name': 'c1', 'host': 'h'})
    mgr = _healthy_manager(api)
    client = api.as_user(root)

    first = client.get('/api/clusters/cluster_1/health')
    assert first.status_code == 200, first.get_data(as_text=True)
    calls_after_first = mgr.get_storage_list.call_count
    assert calls_after_first > 0, 'the first request must actually compute'

    # a second viewer, or the same one a moment later
    second = client.get('/api/clusters/cluster_1/health')
    assert second.status_code == 200
    assert mgr.get_storage_list.call_count == calls_after_first, (
        'the second request recomputed the rollup — the cache is not being used'
    )
    assert second.get_json()['score'] == first.get_json()['score']


def test_repeat_with_matching_etag_gets_304(api, seed):
    root = seed.user('root', role='admin', tenant_id='default')
    seed.db.save_cluster('cluster_1', {'name': 'c1', 'host': 'h'})
    _healthy_manager(api)
    client = api.as_user(root)

    first = client.get('/api/clusters/cluster_1/health')
    etag = first.headers.get('ETag')
    assert etag, 'the route must send an ETag'

    again = client.get('/api/clusters/cluster_1/health',
                       headers={'If-None-Match': etag})
    assert again.status_code == 304, again.get_data(as_text=True)
    assert again.get_data(as_text=True) == ''


def test_route_keeps_its_own_cache_control(api, seed):
    """app.py's after_request stamps `no-store, private` on /api/ responses unless
    the route set Cache-Control itself. A no-store answer can never be revalidated,
    so the ETag would be decorative."""
    root = seed.user('root', role='admin', tenant_id='default')
    seed.db.save_cluster('cluster_1', {'name': 'c1', 'host': 'h'})
    _healthy_manager(api)

    r = api.as_user(root).get('/api/clusters/cluster_1/health')
    assert 'no-store' not in (r.headers.get('Cache-Control') or '')


def test_etag_ignores_computed_at(api):
    """computed_at changes on every computation. If it fed the ETag, every ETag
    would be unique and the conditional request would never match."""
    base = {'score': 100, 'band': 'excellent', 'factors': [], 'issues': []}
    a = dict(base, computed_at='2026-09-04T10:00:00Z')
    b = dict(base, computed_at='2026-09-04T11:22:33Z')
    assert health_etag(a) == health_etag(b)

    changed = dict(base, score=70, computed_at=a['computed_at'])
    assert health_etag(changed) != health_etag(a), 'a different score must change the ETag'


def test_cache_is_per_cluster(api, seed):
    root = seed.user('root', role='admin', tenant_id='default')
    for cid in ('cluster_1', 'cluster_2'):
        seed.db.save_cluster(cid, {'name': cid, 'host': 'h'})
    mgr1 = _healthy_manager(api, 'cluster_1')
    mgr2 = _healthy_manager(api, 'cluster_2')
    client = api.as_user(root)

    client.get('/api/clusters/cluster_1/health')
    assert mgr2.get_storage_list.call_count == 0

    client.get('/api/clusters/cluster_2/health')
    assert mgr2.get_storage_list.call_count > 0, (
        'cluster_2 was served from cluster_1 entry — the cache key ignores the cluster'
    )


def test_expired_entry_is_recomputed(api, seed):
    root = seed.user('root', role='admin', tenant_id='default')
    seed.db.save_cluster('cluster_1', {'name': 'c1', 'host': 'h'})
    mgr = _healthy_manager(api)

    get_cluster_health('cluster_1', mgr)
    calls = mgr.get_storage_list.call_count

    # max_age=0 means "nothing cached is good enough"
    get_cluster_health('cluster_1', mgr, max_age=0)
    assert mgr.get_storage_list.call_count > calls


def test_disconnected_cluster_scores_zero_without_fanout(api):
    """The connectivity gate short-circuits: no point scanning storage on a
    cluster whose API is unreachable."""
    mgr = api.make_fake_manager('cluster_1')
    mgr.is_connected = False

    payload = compute_cluster_health(mgr, 'cluster_1')
    assert payload['score'] == 0
    assert payload['band'] == 'critical'
    assert payload['computed_at'] is None
    assert mgr.get_storage_list.call_count == 0
    assert [f['key'] for f in payload['factors']] == ['api']


def test_offline_node_lowers_the_score(api):
    """Guards the scoring itself through the extraction, not just the caching."""
    # Note the shape: a node counts as online when its status says so OR when it
    # carries no `offline` flag (`... or not n.get('offline')`). So marking one
    # offline needs BOTH keys — status alone would still be counted as online.
    # That is upstream's condition, carried over unchanged by the extraction.
    mgr = api.make_fake_manager(
        'cluster_1',
        get_node_status={'pve1': {'status': 'online'},
                         'pve2': {'status': 'offline', 'offline': True}},
        get_storage_list=[],
        get_replication_status=[],
    )
    mgr.is_connected = True

    payload = compute_cluster_health(mgr, 'cluster_1')
    assert payload['score'] == 75, payload      # one offline node costs 25
    assert payload['band'] == 'good'
    nodes = [f for f in payload['factors'] if f['key'] == 'nodes'][0]
    assert nodes['value'] == '1/2'
    assert any('offline' in i for i in payload['issues'])


def test_ttl_outlives_the_refresher_interval():
    """The refresher is what keeps the cache warm. If entries expire before it
    comes round again, the cache is cold for most of every cycle and whoever asks
    in that window pays the full storage fan-out — which is exactly what a live
    instance showed: responses in either 100ms or 4s, never in between."""
    from pegaprox.core.health import _HEALTH_TTL_S
    from pegaprox.background.health import _INTERVAL_S

    assert _HEALTH_TTL_S > _INTERVAL_S, (
        f'TTL {_HEALTH_TTL_S}s must outlive the refresh interval {_INTERVAL_S}s, '
        'or the cache is cold between rounds'
    )
