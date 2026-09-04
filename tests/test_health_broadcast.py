# The health rollup is pushed once per watched cluster instead of being polled by
# every tab. These tests pin the three properties that make that worth doing:
# a cluster nobody watches costs nothing, an unchanged rollup is not re-sent, and
# one broken cluster does not take the others down with it.

import pytest

import pegaprox.background.health as hb
import pegaprox.globals as ppglobals
from pegaprox.core.health import invalidate_cluster_health
from tests.conftest import make_fake_manager


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Each test gets an empty cluster registry, an empty rollup cache and its own
    list of sent frames — all three are process globals."""
    invalidate_cluster_health()
    hb._last_etag.clear()
    ppglobals.cluster_managers.clear()

    sent = []
    monkeypatch.setattr(hb, 'broadcast_sse',
                        lambda t, data, cid=None, **kw: sent.append((t, cid, data)))
    yield sent

    ppglobals.cluster_managers.clear()
    hb._last_etag.clear()
    invalidate_cluster_health()


def _register(cluster_id='cluster_1', **overrides):
    kw = dict(
        get_node_status={'pve1': {'status': 'online'}},
        get_storage_list=[{'storage': 'local', 'active': 1, 'total': 100, 'used': 10}],
        get_replication_status=[],
    )
    kw.update(overrides)
    mgr = make_fake_manager(cluster_id, **kw)
    mgr.is_connected = True
    ppglobals.cluster_managers[cluster_id] = mgr
    return mgr


def test_watched_cluster_is_computed_and_sent(_isolated, monkeypatch):
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: True)
    mgr = _register()

    hb._broadcast_round(1)

    assert len(_isolated) == 1
    kind, cid, payload = _isolated[0]
    assert kind == 'health'
    assert cid == 'cluster_1'
    assert payload['score'] == 100 and payload['band'] == 'excellent'
    assert mgr.get_storage_list.call_count > 0


def test_unwatched_cluster_costs_nothing(_isolated, monkeypatch):
    """watched_clusters() already gates the resource loop; health must use it too,
    or the saving is spent on clusters nobody has open."""
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: False)
    mgr = _register()

    hb._broadcast_round(1)

    assert _isolated == []
    assert mgr.get_storage_list.call_count == 0, 'an unwatched cluster was computed'


def test_disconnected_cluster_is_skipped(_isolated, monkeypatch):
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: True)
    mgr = _register()
    mgr.is_connected = False

    hb._broadcast_round(1)

    assert _isolated == []
    assert mgr.get_storage_list.call_count == 0


def test_unchanged_rollup_is_not_resent(_isolated, monkeypatch):
    """The ETag doubles as the dedup key: same answer, no frame."""
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: True)
    monkeypatch.setattr(hb, '_KEEPALIVE_ROUNDS', 0)   # keepalive off for this test
    _register()

    hb._broadcast_round(1)
    hb._broadcast_round(2)
    hb._broadcast_round(3)

    assert len(_isolated) == 1, f'resent an unchanged rollup: {len(_isolated)} frames'


def test_changed_rollup_is_sent_again(_isolated, monkeypatch):
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: True)
    monkeypatch.setattr(hb, '_KEEPALIVE_ROUNDS', 0)
    mgr = _register()

    hb._broadcast_round(1)
    mgr.get_node_status.return_value = {
        'pve1': {'status': 'online'},
        'pve2': {'status': 'offline', 'offline': True},
    }
    hb._broadcast_round(2)

    assert len(_isolated) == 2
    assert _isolated[0][2]['score'] != _isolated[1][2]['score']


def test_keepalive_resends_for_late_joiners(_isolated, monkeypatch):
    """A client connecting between two changes would otherwise wait for the next
    one before seeing any value at all."""
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: True)
    monkeypatch.setattr(hb, '_KEEPALIVE_ROUNDS', 3)
    _register()

    for rnd in (1, 2, 3):
        hb._broadcast_round(rnd)

    assert len(_isolated) == 2, 'round 3 should have re-sent despite no change'


def test_unwatching_forgets_the_dedup_state(_isolated, monkeypatch):
    """Otherwise the next viewer is deduped against a frame they never received."""
    watched = {'v': True}
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: watched['v'])
    monkeypatch.setattr(hb, '_KEEPALIVE_ROUNDS', 0)
    _register()

    hb._broadcast_round(1)
    assert len(_isolated) == 1

    watched['v'] = False
    hb._broadcast_round(2)
    watched['v'] = True
    hb._broadcast_round(3)

    assert len(_isolated) == 2, 'the returning viewer got no frame'


def test_one_broken_cluster_does_not_stop_the_others(_isolated, monkeypatch):
    monkeypatch.setattr(hb, 'is_cluster_watched', lambda cid: True)
    broken = _register('cluster_broken')
    broken.get_node_status.side_effect = RuntimeError('PVE unreachable')
    _register('cluster_ok')

    hb._broadcast_round(1)

    sent_for = {cid for _, cid, _ in _isolated}
    assert 'cluster_ok' in sent_for, 'a failing cluster suppressed a healthy one'
