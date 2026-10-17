"""Change plans under attack: four eyes, tenant reach, the state machine and the drift link.

NS Oct 2026 - second round on the Change Guardian. Each test here names one way around the
rules of tests/test_change_plans.py; the ones that found a hole failed before the fix.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from pegaprox.api import changes as changes_api
from pegaprox.core import changes
from pegaprox.utils.auth import create_api_token

from test_change_plans import (CL, OTHER, TENANT_PERMS, _insert, _iso, _make, _plan_body,  # noqa: F401
                               _status, estate, scanned)


def _audit(db, action):
    return [r['user'] for r in db.query('SELECT user FROM audit_log WHERE action = ?', (action,))]


# -- reach through a guest ACL is not a tenant ------------------------------------------------

def _foreign_admin(estate, api, **acl):
    """The administrator of tenant globex (cluster_2). Someone on acme's cluster_1 shared
    guest 150 with them through a VM ACL, which is how they reach cluster_1 at all."""
    user = estate.user('gadmin', role='viewer', tenant_id='globex',
                       tenant_permissions={'globex': {'role': 'admin'}})
    estate.vm_acl(CL, 150, acl.get('users', ['gadmin']))
    return api.as_user(user)


def test_a_tenant_admin_reaching_a_guest_by_acl_does_not_approve_for_its_owner(estate, api, db):
    pid = _make(estate.admin('alice'), 'submitted', kind='guests', targets=(150,))
    gx = _foreign_admin(estate, api)
    # they may look at the plan for the guest they were given ...
    seen = gx.get(f'/api/changes/{pid}')
    assert seen.status_code == 200, seen.get_data(as_text=True)
    assert seen.get_json()['can']['approve'] is False
    assert seen.get_json()['approve_blocked'] == 'out_of_scope'
    # ... but approving it is acme's business, not theirs
    r = gx.post(f'/api/changes/{pid}/approve')
    assert r.status_code == 403 and r.get_json()['code'] == 'OUT_OF_SCOPE', r.get_data(as_text=True)
    assert gx.post(f'/api/changes/{pid}/reject', json={'reason': 'mine now'}).status_code == 403
    assert gx.post(f'/api/changes/{pid}/cancel').status_code == 403
    assert _status(db, pid) == 'submitted'
    assert _audit(db, 'change.approve') == []


def test_a_wildcard_acl_does_not_make_every_tenant_an_approver(estate, api, db):
    pid = _make(estate.admin('alice'), 'submitted', kind='guests', targets=(150,))
    gx = _foreign_admin(estate, api, users=['*'])
    r = gx.post(f'/api/changes/{pid}/approve')
    assert r.status_code == 403, r.get_data(as_text=True)
    assert _status(db, pid) == 'submitted'


def test_the_owning_tenant_admin_still_approves_that_guest(estate, api, db):
    pid = _make(estate.admin('alice'), 'submitted', kind='guests', targets=(150,))
    _foreign_admin(estate, api)
    ta = api.as_user(estate.user('aadmin', role='viewer', tenant_id='acme',
                                 tenant_permissions={'acme': {'role': 'admin'}}))
    r = ta.post(f'/api/changes/{pid}/approve')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _status(db, pid) == 'approved'


# -- the body names no status, no approver, no requester ---------------------------------------

def test_a_status_or_approver_in_the_body_changes_nothing(estate, db):
    alice, bob = estate.admin('alice'), estate.admin('bob')
    forged = {'status': 'approved', 'approved_by': 'bob', 'approved_at': _iso(),
              'requested_by': 'bob', 'cluster_id': OTHER}
    r = alice.post('/api/changes', json=dict(_plan_body(), **dict(forged, cluster_id=CL)))
    assert r.status_code == 201
    pid = r.get_json()['id']
    row = db.query_one('SELECT status, approved_by, requested_by FROM change_plans WHERE id = ?', (pid,))
    assert (row['status'], row['approved_by'], row['requested_by']) == ('draft', '', 'alice')
    # PUT carries none of them either: only the fields of the form, the cluster stays
    assert alice.put(f'/api/changes/{pid}', json=forged).status_code == 400
    alice.put(f'/api/changes/{pid}', json=dict(forged, title='Renamed'))
    row = db.query_one('SELECT status, approved_by, requested_by, cluster_id FROM change_plans WHERE id = ?', (pid,))
    assert (row['status'], row['approved_by'], row['requested_by'], row['cluster_id']) == \
        ('draft', '', 'alice', CL)
    # the approve route takes no name from the body
    assert alice.post(f'/api/changes/{pid}/submit').status_code == 200
    assert bob.post(f'/api/changes/{pid}/approve', json={'approved_by': 'alice'}).status_code == 200
    assert db.query_one('SELECT approved_by FROM change_plans WHERE id = ?', (pid,))['approved_by'] == 'bob'


def test_a_token_of_the_requester_minted_with_a_lower_role_is_still_them(estate, api, db):
    alice = estate.admin('alice')
    pid = _make(alice, 'submitted')
    tok = create_api_token('alice', 'low', role='viewer', expires_days=1)['token']
    r = api.anon().post(f'/api/changes/{pid}/approve', headers={'Authorization': f'Bearer {tok}'})
    assert r.status_code == 403
    assert _status(db, pid) == 'submitted'


def test_an_account_recreated_under_the_requesters_name_does_not_approve(estate, api, db):
    pid = _make(estate.admin('alice'), 'submitted')
    db.delete_user('alice')
    again = api.as_user(estate.user('alice', role='admin'))
    assert again.post(f'/api/changes/{pid}/approve').status_code == 403
    assert _status(db, pid) == 'submitted'


# -- the state machine -------------------------------------------------------------------------

def test_two_approvals_make_one(estate, db):
    alice, bob, carol = estate.admin('alice'), estate.admin('bob'), estate.admin('carol')
    pid = _make(alice, 'submitted')
    assert bob.post(f'/api/changes/{pid}/approve').status_code == 200
    late = carol.post(f'/api/changes/{pid}/approve')
    assert late.status_code == 409 and late.get_json()['code'] == 'INVALID_TRANSITION'
    assert db.query_one('SELECT approved_by FROM change_plans WHERE id = ?', (pid,))['approved_by'] == 'bob'
    assert _audit(db, 'change.approve') == ['bob']


def test_a_cancel_racing_an_approval_loses_cleanly(estate, db, monkeypatch):
    """The approval lands between the cancel's read and its write: the cancel is a 409,
    the plan stays approved, and only the approval is in the audit log."""
    alice, bob = estate.admin('alice'), estate.admin('bob')
    pid = _make(alice, 'submitted')
    real = changes_api._move

    def racing(plan, action, sets):
        if action == 'cancel':
            monkeypatch.setattr(changes_api, '_move', real)
            assert bob.post(f'/api/changes/{pid}/approve').status_code == 200
        return real(plan, action, sets)

    monkeypatch.setattr(changes_api, '_move', racing)
    r = alice.post(f'/api/changes/{pid}/cancel')
    assert r.status_code == 409, r.get_data(as_text=True)
    assert _status(db, pid) == 'approved'
    assert _audit(db, 'change.cancel') == [] and _audit(db, 'change.approve') == ['bob']


def test_every_transition_writes_the_audit_log(estate, db):
    alice, bob = estate.admin('alice'), estate.admin('bob')
    pid = _make(alice)
    assert alice.put(f'/api/changes/{pid}', json={'title': 'Renamed'}).status_code == 200
    assert alice.post(f'/api/changes/{pid}/submit').status_code == 200
    assert bob.post(f'/api/changes/{pid}/approve').status_code == 200
    assert alice.post(f'/api/changes/{pid}/done').status_code == 200
    gone = _make(alice)
    assert bob.post(f'/api/changes/{gone}/cancel').status_code == 200
    rows = db.query("SELECT user, action, cluster_id FROM audit_log WHERE action LIKE 'change.%'")
    got = sorted((r['user'], r['action']) for r in rows)
    assert got == sorted([('alice', 'change.create'), ('alice', 'change.edit'), ('alice', 'change.submit'),
                          ('bob', 'change.approve'), ('alice', 'change.done'),
                          ('alice', 'change.create'), ('bob', 'change.cancel')])
    assert {r['cluster_id'] for r in rows} == {CL}


# -- the drift link ----------------------------------------------------------------------------

def test_a_window_written_in_another_time_zone_links_at_that_moment(scanned, db):
    # 03:00 in Kiritimati (+14) and 03:00 the evening before in Baker Island (-12) are
    # the same 13:00 UTC; the plan covers the hour around now in both spellings
    now = datetime.now(timezone.utc)
    east = (now - timedelta(minutes=30)).astimezone(timezone(timedelta(hours=14)))
    west = (now + timedelta(minutes=30)).astimezone(timezone(timedelta(hours=-12)))
    _insert(db, 'tz', kind='guests', targets=(101,),
            start=changes.iso_utc(changes.parse_utc(east.isoformat())),
            end=changes.iso_utc(changes.parse_utc(west.isoformat())))
    assert scanned()[('vm_config', 'qemu/101')][0] == 'tz'
    # an hour later in the same spelling is outside
    db.conn.execute("UPDATE change_plans SET window_start = ?, window_end = ? WHERE id = 'tz'",
                    (changes.iso_utc(now + timedelta(hours=1)), changes.iso_utc(now + timedelta(hours=2))))
    db.conn.commit()
    assert scanned()[('vm_config', 'qemu/101')][0] is None


def test_a_guest_plan_does_not_cover_the_cluster_or_another_kind(scanned, db):
    # VMIDs that look like names elsewhere: a storage or network scope ending in 101
    _insert(db, 'g', kind='guests', targets=(101, 102))
    got = scanned()
    assert got[('vm_config', 'qemu/101')][0] == 'g' and got[('vm_config', 'qemu/102')][0] == 'g'
    assert {got[k][0] for k in got if k[0] != 'vm_config'} == {None}
