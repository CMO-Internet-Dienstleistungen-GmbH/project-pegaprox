"""Issue #804: PBS job schedules belong beside their datastore.

The existing PBS reads return datastore GC schedules and server-wide sync,
verify and prune jobs. This browser test checks that selecting one datastore
shows only its jobs, in both layouts, using the built frontend bundle.
"""

import re

import pytest

from test_ha_ui import PBS, _App, _FakeServer, browser  # noqa: F401


STORES = [
    {'name': 'alpha', 'gc-schedule': 'daily', 'detail': {'gc-status': {'last-run-endtime': 1780000000}}},
    {'name': 'beta', 'gc-schedule': 'weekly', 'detail': {'gc-status': {}}},
]
JOBS = {
    'sync': [
        {'id': 'sync-alpha', 'store': 'alpha', 'schedule': 'mon 02:00', 'last-run-endtime': 1780000010},
        {'id': 'sync-beta', 'store': 'beta', 'schedule': 'tue 03:00'},
    ],
    'verify': [
        {'id': 'verify-alpha', 'store': 'alpha', 'schedule': 'wed 04:00'},
        {'id': 'verify-beta', 'store': 'beta', 'schedule': 'thu 05:00'},
    ],
    'prune': [
        {'id': 'prune-alpha', 'store': 'alpha', 'schedule': ''},
        {'id': 'prune-beta', 'store': 'beta', 'schedule': 'fri 06:00', 'disable': True},
    ],
}
READS = {
    ('GET', '/api/pbs'): (200, [PBS]),
    ('GET', '/api/pbs/p1/status'): (200, {'version': {'version': '4.0'}}),
    ('GET', '/api/pbs/p1/datastores'): (200, STORES),
    ('GET', '/api/pbs/p1/jobs'): (200, JOBS),
}


@pytest.mark.parametrize('layout', ['modern', 'corporate'])
def test_selected_datastore_shows_only_its_schedules(browser, layout):
    """Changing stores changes the GC schedule and filters each kind of PBS job."""
    app = _App(browser, _FakeServer(role='standalone', layout=layout, extra=READS))
    try:
        page = app.page
        page.get_by_text('backup01').first.click()
        page.get_by_role('button', name='Datastores').first.click()

        page.get_by_role('button', name=re.compile(r'^alpha\b')).click()
        table = page.locator('[data-pbs-datastore-schedules="alpha"]')
        table.wait_for(timeout=5000)
        rows = table.locator('[data-pbs-schedule-kind]')
        assert rows.count() == 4
        assert [r.get_attribute('data-pbs-schedule-kind') for r in rows.all()] == [
            'gc', 'sync', 'verify', 'prune'
        ]
        text = table.inner_text()
        for expected in ('daily', 'sync-alpha', 'mon 02:00', 'verify-alpha', 'prune-alpha', 'Manual'):
            assert expected in text
        assert 'sync-beta' not in text and 'verify-beta' not in text and 'prune-beta' not in text
        assert rows.first.locator('td').nth(3).inner_text() != '-'
        assert rows.nth(1).locator('td').nth(3).inner_text() != '-'

        page.get_by_role('button', name=re.compile(r'^beta\b')).click()
        other = page.locator('[data-pbs-datastore-schedules="beta"]')
        other.wait_for(timeout=5000)
        text = other.inner_text()
        for expected in ('weekly', 'sync-beta', 'verify-beta', 'prune-beta'):
            assert expected in text
        assert 'fri 06:00' in text and 'disabled' in text.lower()
        assert 'sync-alpha' not in text and 'verify-alpha' not in text and 'prune-alpha' not in text
        assert not [call for call in app.server.calls
                    if call[0] != 'GET' and call[1].startswith('/api/pbs/')], app.server.calls
        assert not app.errors, app.errors
    finally:
        app.ctx.close()


def test_gc_schedule_remains_visible_without_configured_jobs(browser):
    """A datastore's GC schedule is useful even when its job list is empty."""
    reads = dict(READS)
    reads[('GET', '/api/pbs/p1/jobs')] = (200, {'sync': [], 'verify': [], 'prune': []})
    app = _App(browser, _FakeServer(role='standalone', extra=reads))
    try:
        page = app.page
        page.get_by_text('backup01').first.click()
        page.get_by_role('button', name='Datastores').first.click()
        page.get_by_role('button', name=re.compile(r'^alpha\b')).click()
        table = page.locator('[data-pbs-datastore-schedules="alpha"]')
        table.wait_for(timeout=5000)
        assert table.locator('[data-pbs-schedule-kind="gc"]').count() == 1
        assert table.locator('[data-pbs-schedule-kind]').count() == 1
        assert 'daily' in table.inner_text()
        assert not app.errors, app.errors
    finally:
        app.ctx.close()


def test_job_read_refusal_is_visible_beside_datastore_schedules(browser):
    """An upstream permission refusal cannot masquerade as no configured jobs."""
    reads = dict(READS)
    reads[('GET', '/api/pbs/p1/jobs')] = (403, {'code': 'PBS_FORBIDDEN',
                                               'error': 'PBS denied the request'})
    app = _App(browser, _FakeServer(role='standalone', extra=reads))
    try:
        page = app.page
        page.get_by_text('backup01').first.click()
        page.get_by_role('button', name='Datastores').first.click()
        page.get_by_role('button', name=re.compile(r'^alpha\b')).click()
        table = page.locator('[data-pbs-datastore-schedules="alpha"]')
        table.wait_for(timeout=5000)
        table.get_by_role('alert').wait_for(timeout=5000)
        assert 'Jobs' in table.get_by_role('alert').inner_text()
        assert table.locator('[data-pbs-schedule-kind="gc"]').count() == 1
        assert table.locator('[data-pbs-schedule-kind="sync"]').count() == 0

        page.get_by_role('button', name='Jobs').first.click()
        assert page.get_by_role('alert').filter(has_text='Jobs').count() > 0
        assert not app.errors, app.errors
    finally:
        app.ctx.close()
