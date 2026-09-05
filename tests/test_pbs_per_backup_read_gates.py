"""The remaining PBS reads that named a guest but never asked who owns it.

Seven routes on this server already resolve the backup's guest and call
_authz_pbs_backup. These five took the same (backup-type, backup-id) pair — or a
UPID that names it — and stopped at check_pbs_access, which only proves the caller
reaches one of the PBS's linked clusters. A datastore is shared across every guest
on every linked cluster, and the permissions involved (pbs.datastore.view,
pbs.tasks.view) are builtin ROLE_USER / ROLE_VIEWER defaults. MK
"""
from unittest.mock import MagicMock

import pytest

import pegaprox.globals as ppglobals


MINE, THEIRS = 100, 200
PBS = 'pbs_a'


def _upid(vmid, store='store1'):
    return f'UPID:pbs:0000A1B2:00003C4D:00005E6F:68AB1F00:backup:{store}:vm/{vmid}/68ab1f00:root@pam:'


@pytest.fixture
def pbs(api):
    m = MagicMock()
    m.linked_clusters = ['cluster_1']
    m.connected = True
    m.get_snapshot_notes.return_value = {'data': 'quarterly ledger export'}
    m.get_group_notes.return_value = {'data': 'finance group'}
    m.get_tasks.return_value = {'data': [
        {'upid': _upid(MINE), 'worker_type': 'backup', 'status': 'OK',
         'worker_id': f'store1:vm/{MINE}/68ab1f00'},
        {'upid': _upid(THEIRS), 'worker_type': 'backup', 'status': 'OK',
         'worker_id': f'store1:vm/{THEIRS}/68ab1f64'},
        {'upid': 'UPID:pbs:1:1:1:1:garbage-collection:store1:root@pam:',
         'worker_type': 'garbage_collection', 'status': 'OK', 'worker_id': 'store1'},
    ]}
    m.get_task_status.return_value = {'data': {'status': 'stopped'}}
    m.get_task_log.return_value = {'data': [{'n': 1, 't': 'archive vm-200-disk-0.img'}]}
    m.get_snapshots.return_value = {'data': []}
    ppglobals.pbs_managers.clear()
    ppglobals.pbs_managers[PBS] = m
    try:
        yield m
    finally:
        ppglobals.pbs_managers.clear()


@pytest.fixture
def scoped(api, seed):
    seed.tenant('tenant_a', clusters=['cluster_1'])
    alice = seed.user('alice', role='user', tenant_id='tenant_a',
                      permissions=['pbs.datastore.view', 'pbs.tasks.view'])
    seed.vm_acl('cluster_1', MINE, ['alice'], permissions=['vm.view'])
    api.set_manager('cluster_1', api.make_fake_manager('cluster_1', get_vm_resources=[]))
    return api.as_user(alice)


@pytest.fixture
def admin(api, seed):
    api.set_manager('cluster_1', api.make_fake_manager('cluster_1', get_vm_resources=[]))
    return api.as_user(seed.user('root_admin', role='admin'))


NOTES = f'/api/pbs/{PBS}/datastores/store1/notes'
GROUP_NOTES = f'/api/pbs/{PBS}/datastores/store1/group-notes'


@pytest.mark.parametrize('path,query', [
    (NOTES, f'?backup-type=vm&backup-id={THEIRS}&backup-time=1756000000'),
    (GROUP_NOTES, f'?backup-type=vm&backup-id={THEIRS}'),
    (f'/api/pbs/{PBS}/backup-diff',
     f'?store=store1&type=vm&id={THEIRS}&a=2026-05-01T03:00:00Z&b=2026-05-08T03:00:00Z'),
])
def test_reads_naming_a_foreign_guest_are_denied(scoped, pbs, path, query):
    r = scoped.get(path + query)

    assert r.status_code == 403, f'{path} -> {r.status_code}: {r.get_data(as_text=True)[:200]}'


@pytest.mark.parametrize('path,query', [
    (NOTES, f'?backup-type=vm&backup-id={MINE}&backup-time=1756000000'),
    (GROUP_NOTES, f'?backup-type=vm&backup-id={MINE}'),
])
def test_reads_for_the_callers_own_guest_still_work(scoped, pbs, path, query):
    r = scoped.get(path + query)

    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert 'notes' in r.get_json()


def test_task_list_is_scoped_to_the_callers_guests(scoped, pbs):
    r = scoped.get(f'/api/pbs/{PBS}/tasks')

    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    ids = [t['worker_id'] for t in r.get_json()]
    assert ids == [f'store1:vm/{MINE}/68ab1f00'], ids


def test_task_detail_for_a_foreign_guest_is_denied(scoped, pbs):
    r = scoped.get(f'/api/pbs/{PBS}/tasks/{_upid(THEIRS)}')

    assert r.status_code == 403, r.get_data(as_text=True)[:200]
    assert 'vm-200-disk-0' not in r.get_data(as_text=True)


def test_task_detail_for_the_callers_own_guest_works(scoped, pbs):
    r = scoped.get(f'/api/pbs/{PBS}/tasks/{_upid(MINE)}')

    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert 'log' in r.get_json()


def test_admin_keeps_every_row_and_every_read(admin, pbs):
    assert admin.get(NOTES + f'?backup-type=vm&backup-id={THEIRS}&backup-time=1').status_code == 200
    assert admin.get(f'/api/pbs/{PBS}/tasks/{_upid(THEIRS)}').status_code == 200
    tasks = admin.get(f'/api/pbs/{PBS}/tasks')
    assert tasks.status_code == 200
    assert len(tasks.get_json()) == 3, 'admin must still see the gc task too'


@pytest.mark.parametrize('upid,expected', [
    (_upid(100), ('vm', '100')),
    ('UPID:pbs:1:1:1:1:backup:store1:ct/205/68ab1f00:root@pam:', ('ct', '205')),
    ('UPID:pbs:1:1:1:1:garbage-collection:store1:root@pam:', (None, None)),
    ('', (None, None)),
    (None, (None, None)),
])
def test_upid_guest_parsing(upid, expected):
    from pegaprox.api.pbs import _pbs_upid_guest      # imported here so the rest of the file
    assert _pbs_upid_guest(upid) == expected          # still collects without it
