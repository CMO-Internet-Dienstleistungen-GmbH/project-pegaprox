# POST /api/sse/subscribe stored the body's `clusters` value in the shared client registry
# as sent whenever the caller's cluster scope was unrestricted (admins, and default-tenant
# users without a cluster list). The broadcaster aggregates every subscription with
# set.update(), so `[{}]` raised there on every tick and stalled live updates for all
# clients while the poisoning stream stayed open. The WebSocket subscribe message had the
# same gap. Malformed lists are now refused before they reach the registry. MK Oct 2026

import queue

import pytest

import pegaprox.globals as ppglobals
from pegaprox.api.realtime import _cluster_id_list
from pegaprox.utils.realtime import watched_clusters


@pytest.fixture
def sse_client():
    added = []

    def _add(username, clusters=None):
        cid = f'test-{username}-{len(added)}'
        with ppglobals.sse_clients_lock:
            ppglobals.sse_clients[cid] = {'queue': queue.Queue(), 'user': username, 'clusters': clusters}
        added.append(cid)
        return cid

    yield _add
    with ppglobals.sse_clients_lock:
        for cid in added:
            ppglobals.sse_clients.pop(cid, None)


@pytest.mark.parametrize('clusters', [[{}], [['a']], 'cluster_1', {'cluster_1': 1}, [1], ['']])
def test_a_malformed_list_is_a_400_and_the_registry_is_untouched(api, seed, sse_client, clusters):
    admin = seed.user('root2', role='admin')
    cid = sse_client('root2', clusters=['cluster_1'])

    r = api.as_user(admin).post('/api/sse/subscribe', json={'client_id': cid, 'clusters': clusters})

    assert r.status_code == 400, r.get_data(as_text=True)
    assert ppglobals.sse_clients[cid]['clusters'] == ['cluster_1']
    assert watched_clusters() is None or 'cluster_1' in watched_clusters()


def test_a_well_formed_list_is_stored(api, seed, sse_client):
    admin = seed.user('root2', role='admin')
    cid = sse_client('root2', clusters=['cluster_1'])

    r = api.as_user(admin).post('/api/sse/subscribe',
                                json={'client_id': cid, 'clusters': ['cluster_1', 'cluster_2']})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert ppglobals.sse_clients[cid]['clusters'] == ['cluster_1', 'cluster_2']


def test_null_still_means_everything_the_caller_may_see(api, seed, sse_client):
    admin = seed.user('root2', role='admin')
    cid = sse_client('root2', clusters=['cluster_1'])

    r = api.as_user(admin).post('/api/sse/subscribe', json={'client_id': cid, 'clusters': None})

    assert r.status_code == 200, r.get_data(as_text=True)
    assert ppglobals.sse_clients[cid]['clusters'] is None


def test_a_non_string_client_id_is_a_400(api, seed):
    admin = seed.user('root2', role='admin')
    r = api.as_user(admin).post('/api/sse/subscribe', json={'client_id': {'x': 1}, 'clusters': None})
    assert r.status_code == 400


def test_the_websocket_check_matches():
    assert _cluster_id_list(None) is None
    assert _cluster_id_list(['cluster_1']) == ['cluster_1']
    assert _cluster_id_list([{}]) is False
    assert _cluster_id_list('cluster_1') is False
    assert _cluster_id_list(['c'] * 1001) is False
