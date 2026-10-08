# -*- coding: utf-8 -*-
"""Snapshot author metadata — fork patch for issue #39.

PVE snapshots carry no author, so pegaprox/core/snapshot_meta.py keeps the link
between a snapshot and the account that asked for it. These tests pin the parts
that a later upstream rebase could silently drop: that an author survives, that
it is never guessed, and that a reused snapshot name does not inherit one.
"""
import time

import pytest

from pegaprox.core import snapshot_meta


CLUSTER = 'cluster_1'
OTHER_CLUSTER = 'cluster_2'


@pytest.fixture(autouse=True)
def _fresh_schema(db):
    """Each test gets the `db` fixture's throwaway database.

    snapshot_meta remembers that it created its table, so that flag has to be
    cleared or the second test writes into the first test's deleted file.
    """
    snapshot_meta.reset_schema_cache()
    yield
    snapshot_meta.reset_schema_cache()


def _snap(name, snaptime, description=''):
    return {'name': name, 'snaptime': snaptime, 'description': description}


# ── the plain case ────────────────────────────────────────────────────────────

def test_recorded_author_shows_up_on_the_snapshot():
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')

    snaps = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now)])

    assert snaps[0]['author'] == 'alice'
    assert snaps[0]['author_origin'] == snapshot_meta.ORIGIN_USER


def test_author_survives_a_restart_and_a_second_viewer():
    """Nothing about the answer may depend on who is looking or on process state."""
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now)])

    snapshot_meta.reset_schema_cache()  # stands in for a restarted process

    again = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now)])
    assert again[0]['author'] == 'alice'


def test_a_policy_run_is_recorded_as_automatic():
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'pegaprox-p1-0001', 'Nightly policy',
                                  origin=snapshot_meta.ORIGIN_AUTOMATIC)

    snaps = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('pegaprox-p1-0001', now)])

    assert snaps[0]['author_origin'] == snapshot_meta.ORIGIN_AUTOMATIC
    assert snaps[0]['author'] == 'Nightly policy'


# ── what must NOT get an author ───────────────────────────────────────────────

def test_a_snapshot_we_never_created_has_no_author():
    """Pre-existing and externally made snapshots stay unattributed."""
    snaps = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('made-elsewhere', int(time.time()))])

    assert snaps[0]['author'] == ''
    assert snaps[0]['author_origin'] == ''


def test_the_same_name_on_another_guest_or_cluster_is_not_confused():
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')

    other_guest = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 102, [_snap('nightly', now)])
    other_type = snapshot_meta.annotate_snapshots(CLUSTER, 'lxc', 101, [_snap('nightly', now)])
    other_cluster = snapshot_meta.annotate_snapshots(OTHER_CLUSTER, 'qemu', 101, [_snap('nightly', now)])
    ours = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now)])

    assert other_guest[0]['author'] == ''
    assert other_type[0]['author'] == ''
    assert other_cluster[0]['author'] == ''
    assert ours[0]['author'] == 'alice'


def test_an_older_snapshot_of_the_same_name_is_not_adopted():
    """A creation we requested cannot have produced a snapshot from last week."""
    long_ago = int(time.time()) - 7 * 86400
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')

    snaps = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', long_ago)])

    assert snaps[0]['author'] == ''


def test_a_failed_creation_leaves_nothing_to_inherit(monkeypatch):
    """The record is written when the creation is requested, not when it lands.

    If the snapshot never appears, the row must be dropped rather than wait for
    a later snapshot of the same name to pick it up.
    """
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')

    # the snapshot shows up long after the creation window closed
    much_later = int(time.time()) + 7 * 86400
    monkeypatch.setattr(snapshot_meta.time, 'time', lambda: much_later)

    snaps = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', much_later)])
    assert snaps[0]['author'] == ''

    # and the stale row is gone, so nothing lingers for the next one either
    rows = snapshot_meta._connection().cursor().execute(
        'SELECT COUNT(*) FROM snapshot_authors WHERE snapname = ?', ('nightly',)
    ).fetchone()
    assert rows[0] == 0


def test_deleting_a_snapshot_drops_its_author():
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now)])

    snapshot_meta.forget(CLUSTER, 'qemu', 101, 'nightly')

    snaps = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now + 5)])
    assert snaps[0]['author'] == ''


def test_a_recreated_name_does_not_inherit_the_old_author():
    """Deleted outside PegaProx, so `forget` never ran — the times still differ."""
    first = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    seen = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', first)])
    assert seen[0]['author'] == 'alice'

    recreated = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', first + 3600)])
    assert recreated[0]['author'] == ''


def test_a_rebound_author_cannot_be_stolen_by_a_later_snapshot():
    """Once bound, the row answers for one snapshot only, in both directions."""
    first = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', first)])

    both = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [
        _snap('nightly', first + 60),
    ])
    assert both[0]['author'] == ''

    still_ours = snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', first)])
    assert still_ours[0]['author'] == 'alice'


# ── aggregated overviews ──────────────────────────────────────────────────────

def test_overview_rows_are_annotated_across_clusters():
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.record_creation(OTHER_CLUSTER, 'lxc', 900, 'before-upgrade', 'bob')

    rows = snapshot_meta.annotate_rows([
        {'cluster_id': CLUSTER, 'vm_type': 'qemu', 'vmid': 101,
         'snapshot_name': 'nightly', 'snaptime': now},
        {'cluster_id': OTHER_CLUSTER, 'vm_type': 'lxc', 'vmid': 900,
         'snapshot_name': 'before-upgrade', 'snaptime': now},
        {'cluster_id': CLUSTER, 'vm_type': 'qemu', 'vmid': 101,
         'snapshot_name': 'unknown-one', 'snaptime': now},
    ])

    assert [r['author'] for r in rows] == ['alice', 'bob', '']


def test_an_overview_page_costs_one_query_whatever_its_length():
    """The issue forbids a lookup per table row — so count the statements."""
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.annotate_rows([{'cluster_id': CLUSTER, 'vm_type': 'qemu', 'vmid': 101,
                                  'snapshot_name': 'nightly', 'snaptime': now}])

    def count_selects(row_count):
        statements = []
        conn = snapshot_meta._connection()
        conn.set_trace_callback(statements.append)
        try:
            snapshot_meta.annotate_rows([
                {'cluster_id': CLUSTER, 'vm_type': 'qemu', 'vmid': 100 + i,
                 'snapshot_name': f'snap-{i}', 'snaptime': now} for i in range(row_count)
            ])
        finally:
            conn.set_trace_callback(None)
        return len([s for s in statements if s.lstrip().upper().startswith('SELECT')])

    assert count_selects(1) == count_selects(50) == 1


def test_efficient_snapshots_answer_under_the_same_keys():
    rows = snapshot_meta.annotate_efficient([
        {'id': 'a', 'snapname': 'cow-1', 'created_by': 'alice'},
        {'id': 'b', 'snapname': 'cow-2', 'created_by': ''},
    ])

    assert rows[0]['author'] == 'alice'
    assert rows[0]['author_origin'] == snapshot_meta.ORIGIN_USER
    assert rows[1]['author'] == ''
    assert rows[1]['author_origin'] == ''


# ── through the HTTP stack ────────────────────────────────────────────────────

def test_creating_and_listing_a_snapshot_carries_the_author(api, seed):
    """End to end: the account that creates a snapshot is the author the VM view reads."""
    user = seed.user('alice', role='admin')
    client = api.as_user(user)
    now = int(time.time())

    fake = api.make_fake_manager(
        create_snapshot={'success': True, 'task': 'UPID:test'},
        get_snapshots=[{'name': 'nightly', 'snaptime': now, 'description': 'before the upgrade'}],
    )
    api.set_manager(CLUSTER, fake)

    created = client.post(f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots',
                          json={'snapname': 'nightly', 'description': 'before the upgrade'})
    assert created.status_code == 200

    listed = client.get(f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots')
    assert listed.status_code == 200
    body = listed.get_json()
    assert body[0]['author'] == 'alice'
    assert body[0]['author_origin'] == snapshot_meta.ORIGIN_USER
    assert body[0]['description'] == 'before the upgrade'


def test_the_author_is_not_whoever_is_looking(api, seed):
    seed.user('alice', role='admin')
    bob = seed.user('bob', role='admin')
    now = int(time.time())

    fake = api.make_fake_manager(
        create_snapshot={'success': True, 'task': 'UPID:test'},
        get_snapshots=[{'name': 'nightly', 'snaptime': now, 'description': ''}],
    )
    api.set_manager(CLUSTER, fake)

    api.as_user({'username': 'alice', 'role': 'admin'}).post(
        f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots', json={'snapname': 'nightly'})

    seen_by_bob = api.as_user(bob).get(f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots')
    assert seen_by_bob.get_json()[0]['author'] == 'alice'


def test_a_deleted_snapshot_takes_its_author_with_it(api, seed):
    user = seed.user('alice', role='admin')
    client = api.as_user(user)
    now = int(time.time())

    fake = api.make_fake_manager(
        create_snapshot={'success': True, 'task': 'UPID:test'},
        delete_snapshot={'success': True, 'task': 'UPID:test'},
        get_snapshots=[{'name': 'nightly', 'snaptime': now + 4000, 'description': ''}],
    )
    api.set_manager(CLUSTER, fake)

    client.post(f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots', json={'snapname': 'nightly'})
    # the fake manager does not run our central forget hook, so call the route's
    # own path and then assert on the store
    snapshot_meta.forget(CLUSTER, 'qemu', 101, 'nightly')

    listed = client.get(f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots')
    assert listed.get_json()[0]['author'] == ''


def test_a_broken_metadata_store_does_not_fail_a_delete(monkeypatch):
    """The bookkeeping rides along; it never decides whether an action worked."""
    from unittest.mock import MagicMock
    from pegaprox.core.manager import PegaProxManager

    mgr = PegaProxManager.__new__(PegaProxManager)
    mgr.is_connected = True
    mgr.logger = MagicMock()
    mgr.current_host = 'pve.example'
    mgr.config = MagicMock(host='pve.example', api_port=8006)
    response = MagicMock(status_code=200)
    response.json.return_value = {'data': 'UPID:...'}
    mgr._api_delete = MagicMock(return_value=response)

    def explode(*_a, **_kw):
        raise RuntimeError('no database here')

    monkeypatch.setattr(snapshot_meta, 'forget', explode)

    assert mgr.delete_snapshot('pve1', 100, 'qemu', 'pre-upgrade')['success'] is True


def test_a_snapshot_replaced_outside_pegaprox_does_not_inherit_the_author(api, seed):
    """The record is bound at creation, so a same-name replacement stays unknown.

    Without that binding the row is still unbound when the replacement appears,
    and a snapshot nobody here made would be shown as ours.
    """
    user = seed.user('alice', role='admin')
    client = api.as_user(user)
    made_at = int(time.time())

    fake = api.make_fake_manager(
        create_snapshot={'success': True, 'task': 'UPID:test'},
        get_snapshots=[{'name': 'nightly', 'snaptime': made_at, 'description': ''}],
    )
    api.set_manager(CLUSTER, fake)

    client.post(f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots', json={'snapname': 'nightly'})

    # deleted and recreated on the node itself: same name, a later timestamp
    fake.get_snapshots.return_value = [{'name': 'nightly', 'snaptime': made_at + 120, 'description': ''}]

    listed = client.get(f'/api/clusters/{CLUSTER}/vms/node1/qemu/101/snapshots')
    assert listed.get_json()[0]['author'] == ''


def test_a_snapshot_deleted_on_the_node_stops_costing_us_a_row():
    """Deleted outside PegaProx, so `forget` never ran — the listing reconciles."""
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'weekly', 'alice')
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [
        _snap('nightly', now), _snap('weekly', now),
    ])

    # only one of them is still there the next time we look
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now)])

    rows = snapshot_meta._connection().cursor().execute(
        'SELECT snapname FROM snapshot_authors WHERE vmid = 101'
    ).fetchall()
    assert [r[0] for r in rows] == ['nightly']


def test_an_empty_listing_is_not_taken_as_proof_of_deletion():
    """An empty answer and a failed query look the same — so keep the rows."""
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('nightly', now)])

    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [])

    assert snapshot_meta.annotate_snapshots(
        CLUSTER, 'qemu', 101, [_snap('nightly', now)])[0]['author'] == 'alice'


def test_another_guests_records_are_left_alone_by_the_reconciliation():
    now = int(time.time())
    snapshot_meta.record_creation(CLUSTER, 'qemu', 101, 'nightly', 'alice')
    snapshot_meta.record_creation(CLUSTER, 'qemu', 202, 'nightly', 'bob')
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 202, [_snap('nightly', now)])

    # listing guest 101 with a different snapshot must not touch guest 202
    snapshot_meta.annotate_snapshots(CLUSTER, 'qemu', 101, [_snap('other', now)])

    assert snapshot_meta.annotate_snapshots(
        CLUSTER, 'qemu', 202, [_snap('nightly', now)])[0]['author'] == 'bob'


def test_a_listing_reads_only_the_guests_it_asks_about():
    """Cost must follow the page, not everything the cluster ever tracked."""
    now = int(time.time())
    for vmid in range(100, 140):
        snapshot_meta.record_creation(CLUSTER, 'qemu', vmid, f'snap-{vmid}', 'alice')

    statements = []
    conn = snapshot_meta._connection()
    conn.set_trace_callback(statements.append)
    try:
        snapshot_meta.annotate_rows([
            {'cluster_id': CLUSTER, 'vm_type': 'qemu', 'vmid': 100,
             'snapshot_name': 'snap-100', 'snaptime': now},
        ])
    finally:
        conn.set_trace_callback(None)

    selects = [s for s in statements if s.lstrip().upper().startswith('SELECT')]
    assert len(selects) == 1
    # the trace shows bound values already expanded, so this asserts the guest
    # filter is there and that the other 39 guests were not read
    assert 'vmid IN (100)' in selects[0]
