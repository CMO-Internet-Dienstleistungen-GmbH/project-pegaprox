# -*- coding: utf-8 -*-
"""The migration list has to be usable after something has happened in it.

Two faults, both reported from a real failed migration:

* the phase timeline ticked the step the run died in. A failed phase still gets an end
  time — that is what "the phase is over" means — and the timeline read an end as a pass,
  so a transfer that converted nothing was shown with a checkmark beside it;
* nothing could be taken off the list. The log of the selected migration is rendered below
  the list, so every entry an operator has already read sits between them and the next
  reason they need to see. Entries left only on a timer, six hours after they finished.
"""

import os

import pytest

from pegaprox.api import xhm as xhm_api


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Task:
    def __init__(self, mid, status='completed'):
        self.id = mid
        self.status = status
        self.source_cluster = 'c1'
        self.target_cluster = 'c1'

    def to_dict(self):
        return {'id': self.id, 'status': self.status}


@pytest.fixture
def registry(monkeypatch):
    tasks = {}
    monkeypatch.setattr(xhm_api, '_xhm_migrations', tasks)
    monkeypatch.setattr(xhm_api, '_xhm_reachable', lambda t: True)
    return tasks


def test_one_finished_migration_can_be_taken_off_the_list(api, seed, registry):
    admin = seed.user('root', role='admin')
    registry['done1'] = _Task('done1')

    resp = api.as_user(admin).delete('/api/xhm/migrations/done1')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert 'done1' not in registry


def test_a_running_migration_is_refused_rather_than_dropped(api, seed, registry):
    """Dropping its record would leave a transfer writing with nothing on screen."""
    admin = seed.user('root', role='admin')
    registry['live'] = _Task('live', status='running')

    resp = api.as_user(admin).delete('/api/xhm/migrations/live')

    assert resp.status_code == 409
    assert 'live' in registry


def test_clearing_finished_leaves_the_running_one_alone(api, seed, registry):
    admin = seed.user('root', role='admin')
    registry['done1'] = _Task('done1')
    registry['died'] = _Task('died', status='failed')
    registry['live'] = _Task('live', status='running')

    resp = api.as_user(admin).delete('/api/xhm/migrations')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert sorted(resp.get_json()['removed']) == ['died', 'done1']
    assert list(registry) == ['live']


def test_an_unknown_migration_is_a_404_not_a_crash(api, seed, registry):
    admin = seed.user('root', role='admin')
    assert api.as_user(admin).delete('/api/xhm/migrations/nope').status_code == 404


def test_the_timeline_does_not_tick_the_step_that_failed():
    """Read out of the source: the timeline lives in a component this suite cannot mount."""
    with open(os.path.join(REPO, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
        source = fh.read()

    start = source.index("const phases = ['planning','transfer','creating','attaching','completed'];")
    block = source[start:start + 12000]

    assert 'brokeAt' in block, (
        'nothing distinguishes the phase a failed run died in, so it is drawn as passed')
    assert 'const isDone = pt && pt.end && !broke;' in block


def test_the_list_cannot_push_the_log_off_the_screen():
    with open(os.path.join(REPO, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
        source = fh.read()
    start = source.index('{xhmMigrations.length > 0 ? (')
    assert 'overflow-y-auto' in source[start:start + 900], (
        'the list is unbounded again; the log of the selected migration is below it')
