# Fork patch #11 on top of upstream 1.5.0 (#1218, #948).
#
# Upstream scopes the health rollup per caller: an account confined to some guests is told
# about the nodes those run on, and nothing about Ceph. The patch caches and broadcasts one
# rollup per cluster, so these tests pin where that one rollup may go and where it may not:
#
#   * a confined caller is answered its own rollup and never the cached one, and its rollup
#     never enters the cache;
#   * the SSE 'health' frame reaches admin and unconfined streams only;
#   * a cached rollup is not served again once the Ceph state it was scored on has changed.

import queue

import pytest

import pegaprox.api.clusters as clusters_mod
import pegaprox.core.health as health_mod
import pegaprox.utils.realtime as rt
from pegaprox.core.cache import StorageDataCache
from pegaprox.core.health import get_cluster_health, invalidate_cluster_health, peek_cluster_health

CID = 'cluster_1'
ROUTE = f'/api/clusters/{CID}/health'


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    invalidate_cluster_health()
    monkeypatch.setattr(clusters_mod, '_health_storage_cache', StorageDataCache())
    yield
    invalidate_cluster_health()


def _manager(api, nodes=('pve1', 'pve2', 'pve3'), offline=('pve3',)):
    node_status = {n: ({'status': 'offline', 'offline': True} if n in offline else {'status': 'online'})
                   for n in nodes}
    mgr = api.make_fake_manager(
        CID, cluster_type='xcpng',
        get_node_status=node_status,
        get_storage_list=[{'storage': 'local', 'active': 1, 'total': 100, 'used': 10}],
        get_replication_status=[],
    )
    mgr.is_connected = True
    return api.set_manager(CID, mgr)


@pytest.fixture
def client(api, seed):
    seed.db.save_cluster(CID, {'name': 'c1', 'host': 'h'})
    return api.as_user(seed.user('root', role='admin', tenant_id='default'))


def _nodes_factor(body):
    return next(f for f in body['factors'] if f['key'] == 'nodes')


def test_a_confined_caller_gets_a_rollup_of_its_own_nodes(api, client, monkeypatch):
    _manager(api)
    monkeypatch.setattr(clusters_mod, 'caller_guest_nodes', lambda cid: {'pve1'})

    body = client.get(ROUTE).get_json()

    assert _nodes_factor(body)['value'] == '1/1'
    assert body['issues'] == []


def test_a_confined_rollup_never_enters_the_shared_cache(api, client, monkeypatch):
    _manager(api)
    monkeypatch.setattr(clusters_mod, 'caller_guest_nodes', lambda cid: {'pve1'})

    client.get(ROUTE)

    assert peek_cluster_health(CID) is None


def test_a_confined_caller_is_never_served_the_cached_rollup(api, client, monkeypatch):
    mgr = _manager(api)
    get_cluster_health(CID, mgr)   # an unconfined viewer warmed the cache
    assert _nodes_factor(peek_cluster_health(CID)[0])['value'] == '2/3'
    monkeypatch.setattr(clusters_mod, 'caller_guest_nodes', lambda cid: {'pve1'})

    body = client.get(ROUTE).get_json()

    assert _nodes_factor(body)['value'] == '1/1'
    assert 'pve3' not in ' '.join(body['issues'])


def test_an_unconfined_caller_is_served_from_the_cache(api, client, monkeypatch):
    mgr = _manager(api)
    monkeypatch.setattr(clusters_mod, 'caller_guest_nodes', lambda cid: None)

    client.get(ROUTE)
    calls = mgr.get_node_status.call_count
    client.get(ROUTE)

    assert peek_cluster_health(CID) is not None
    assert mgr.get_node_status.call_count == calls


def test_unreadable_guests_leave_the_node_factors_out(api, client, monkeypatch):
    _manager(api)

    def unreadable(cid):
        raise RuntimeError('guests unread')
    monkeypatch.setattr(clusters_mod, 'caller_guest_nodes', unreadable)

    body = client.get(ROUTE).get_json()

    assert not any(f['key'] == 'nodes' for f in body['factors'])
    assert peek_cluster_health(CID) is None


# ---------------------------------------------------------------------------
# The SSE frame
# ---------------------------------------------------------------------------

@pytest.fixture
def streams(monkeypatch):
    saved = dict(rt.sse_clients)
    rt.sse_clients.clear()
    queues = {}

    def add(name, scope, is_admin=False):
        q = queues[name] = queue.Queue()
        rt.sse_clients[name] = {'queue': q, 'user': name, 'clusters': [CID],
                                'is_admin': is_admin, 'effective_role': 'user'}
        return scope

    scopes = {}
    monkeypatch.setattr(rt, '_sse_node_scope_for',
                        lambda cid, uname, eff=None, user=None: scopes[uname])
    yield queues, lambda name, scope, is_admin=False: scopes.__setitem__(name, add(name, scope, is_admin))
    rt.sse_clients.clear()
    rt.sse_clients.update(saved)


def _got(q):
    return not q.empty()


def test_the_health_frame_reaches_admin_and_unconfined_streams_only(streams):
    queues, add = streams
    add('admin', 'unused', is_admin=True)
    add('operator', None)
    add('confined', frozenset({'pve1'}))
    add('no_view', False)

    rt.broadcast_sse('health', {'score': 50, 'band': 'warning', 'factors': [], 'issues': []}, CID)

    assert _got(queues['admin']) and _got(queues['operator'])
    assert not _got(queues['confined'])
    assert not _got(queues['no_view'])


def test_other_frames_still_reach_a_confined_stream(streams):
    queues, add = streams
    add('confined', frozenset({'pve1'}))

    rt.broadcast_sse('migration', {'id': 'm1'}, CID)

    assert _got(queues['confined'])


# ---------------------------------------------------------------------------
# The Ceph state a cached rollup was scored on
# ---------------------------------------------------------------------------

def test_a_new_ceph_read_retires_the_cached_rollup(api, monkeypatch):
    mgr = _manager(api)
    mark: list = [(None, None)]
    monkeypatch.setattr(health_mod, '_ceph_mark', lambda cid: mark[0])

    get_cluster_health(CID, mgr)
    calls = mgr.get_node_status.call_count
    _, _, hit = get_cluster_health(CID, mgr)
    assert hit and mgr.get_node_status.call_count == calls

    mark[0] = (1234.0, None)   # a Ceph read landed
    _, _, hit = get_cluster_health(CID, mgr)

    assert not hit and mgr.get_node_status.call_count > calls


def test_a_rollup_scored_before_ceph_ever_answered_is_not_kept(api, monkeypatch):
    mgr = _manager(api)
    monkeypatch.setattr(health_mod, '_ceph_mark', lambda cid: (None, None))
    clusters_mod._health_storage_cache.set(CID, 'resources', [{'plugintype': 'rbd'}], ttl_seconds=60)

    get_cluster_health(CID, mgr)
    _, _, hit = get_cluster_health(CID, mgr)

    assert not hit


def test_a_cluster_without_ceph_storage_keeps_its_rollup(api, monkeypatch):
    mgr = _manager(api)
    monkeypatch.setattr(health_mod, '_ceph_mark', lambda cid: (None, None))
    clusters_mod._health_storage_cache.set(CID, 'resources', [{'plugintype': 'zfspool'}], ttl_seconds=60)

    get_cluster_health(CID, mgr)
    _, _, hit = get_cluster_health(CID, mgr)

    assert hit
