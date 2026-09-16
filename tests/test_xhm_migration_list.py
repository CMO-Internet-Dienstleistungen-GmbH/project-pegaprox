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


class TestDismissingReachesADurableRecord:
    """The list is built from a dict in this process, but a migration can also be recorded
    somewhere that survives a restart. Dropping only the in-memory entry makes the row
    come back on the next page load, with no explanation."""

    def test_a_product_without_durable_records_is_unaffected(self, monkeypatch):
        # The import is optional on purpose: this patch must work on its own.
        import builtins
        real_import = builtins.__import__

        def no_hyperv(name, *args, **kwargs):
            if name == 'pegaprox.core.hyperv_xhm':
                raise ImportError('not part of this build')
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, '__import__', no_hyperv)
        from pegaprox.api import xhm

        assert list(xhm._forget_recorded('mig1')) == []

    def test_a_record_that_still_holds_something_is_reported_not_dropped(self, monkeypatch):
        # The module is injected rather than imported: this patch ships without it, which
        # is the whole reason the import is optional.
        from pegaprox.api import xhm

        _install_fake_record_keeper(monkeypatch,
                                   lambda mid: {'forgotten': False,
                                                'error': 'still has 1 resource(s)'})

        assert list(xhm._forget_recorded('mig1')) == [('mig1', 'still has 1 resource(s)')]

    def test_a_record_with_nothing_left_goes(self, monkeypatch):
        from pegaprox.api import xhm

        _install_fake_record_keeper(monkeypatch, lambda mid: {'forgotten': True})

        assert list(xhm._forget_recorded('mig1')) == [('mig1', None)]

    def test_dismissing_everything_asks_about_each_finished_record(self, monkeypatch):
        from pegaprox.api import xhm

        asked = []
        _install_fake_record_keeper(
            monkeypatch,
            lambda mid: (asked.append(mid), {'forgotten': True})[1],
            recorded=[{'id': 'mig1', 'status': 'failed'},
                      {'id': 'mig2', 'status': 'running'}])

        assert list(xhm._forget_recorded(None)) == [('mig1', None)]
        # The running one is never offered up: its record is still in use.
        assert asked == ['mig1']


def _install_fake_record_keeper(monkeypatch, forget, recorded=()):
    """Stand in for the fork's Hyper-V module, which this patch does not depend on."""
    import sys
    import types

    module = types.ModuleType('pegaprox.core.hyperv_xhm')
    module.forget_recorded_migration = forget
    module.recorded_migrations = lambda known: list(recorded)
    monkeypatch.setitem(sys.modules, 'pegaprox.core.hyperv_xhm', module)
