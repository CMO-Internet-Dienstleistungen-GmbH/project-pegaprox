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
    assert "const isDone = !broke && !!pt && (" in block


def _dashboard():
    with open(os.path.join(REPO, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
        return fh.read()


def _migration_list(source):
    start = source.index('{xhmMigrations.length > 0 ? (')
    return source[start:source.index("t('xhmNoMigrations')", start)]


class TestAMigrationOpensInPlace:
    """Read out of the source: the list lives in a component this suite cannot mount.

    The log of the selected migration used to be a panel below the whole list, so every
    entry above pushed it further down. Each row now opens under itself, with the phase
    bar and the log in it."""

    def test_the_phase_bar_and_the_log_are_inside_the_row(self):
        rows = _migration_list(_dashboard())
        assert 'xhmSelectedMigration === m.id && (() =>' in rows
        opened = rows[rows.index('xhmSelectedMigration === m.id && (() =>'):]
        assert 'phases.map(' in opened
        assert '(d.log || []).map(' in opened

    def test_there_is_no_log_panel_below_the_list_any_more(self):
        assert 'Migration Log - {xhmMigrationDetail.vm_name}' not in _dashboard()

    def test_a_row_shows_whether_it_is_open(self):
        rows = _migration_list(_dashboard())
        assert "Icons.ChevronRight" in rows
        assert "rotate-90" in rows

    def test_a_completed_run_ticks_its_last_phase(self):
        """"completed" is where a run ends, so it never gets an end time. The bar drew
        it as step 5, not reached, under a migration that says completed."""
        rows = _migration_list(_dashboard())
        assert "ph === 'completed' && (d.status === 'completed' ||" in rows

    def test_each_phase_is_named_under_its_step(self):
        rows = _migration_list(_dashboard())
        assert '{phaseLabel[ph]}' in rows

    def test_a_phase_frame_does_not_cut_the_open_log_back_to_30_lines(self):
        source = _dashboard()
        assert 'log: xhmMergeLog(prev.log, m.log)' in source
        assert 'setXhmMigrationDetail(prev => prev ? {...prev, ...m} : m)' not in source

    def test_lines_the_server_did_not_send_live_are_fetched_afterwards(self):
        """XHMigrationTask.log sends at most one frame per second and drops the others."""
        source = _dashboard()
        assert source.count('xhmRefetchDetailSoon(m.id)') >= 2


def test_the_log_route_returns_the_whole_log(api, seed, registry):
    """to_dict() keeps the last 30 lines. After a reload the open log showed only what the
    last phase had written."""
    admin = seed.user('root', role='admin')
    task = _Task('long1')
    task.log_lines = [f'[00:00:{i:02d}] line {i}' for i in range(120)]
    task.to_dict = lambda: {'id': 'long1', 'status': 'completed', 'log': task.log_lines[-30:]}
    registry['long1'] = task

    resp = api.as_user(admin).get('/api/xhm/migrations/long1/log')

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json() == {'id': 'long1', 'log': task.log_lines}


def test_an_opened_row_loads_the_whole_log():
    with open(os.path.join(REPO, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
        source = fh.read()
    assert '/xhm/migrations/${mid}/log' in source


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
                                                'error': 'still has 1 resource(s)'},
                                   recorded=[{'id': 'mig1', 'status': 'failed'}])

        assert list(xhm._forget_recorded('mig1')) == [('mig1', 'still has 1 resource(s)')]

    def test_a_record_with_nothing_left_goes(self, monkeypatch):
        from pegaprox.api import xhm

        _install_fake_record_keeper(monkeypatch, lambda mid: {'forgotten': True},
                                   recorded=[{'id': 'mig1', 'status': 'failed'}])

        assert list(xhm._forget_recorded('mig1')) == [('mig1', None)]

    def test_a_record_the_caller_may_not_see_is_not_touched(self, monkeypatch):
        from pegaprox.api import xhm

        asked = []
        _install_fake_record_keeper(
            monkeypatch, lambda mid: (asked.append(mid), {'forgotten': True})[1],
            recorded=[{'id': 'mine', 'status': 'failed', 'source_cluster': 'mine'},
                      {'id': 'theirs', 'status': 'failed', 'source_cluster': 'theirs'}],
            may_see=lambda row: row.get('source_cluster') == 'mine')

        assert list(xhm._forget_recorded(None)) == [('mine', None)]
        assert list(xhm._forget_recorded('theirs')) == []
        assert asked == ['mine']


class TestDismissingARowThatOnlyTheRecordKnows:
    """After a restart the list shows recorded migrations that are not in this process.
    The X on such a row answered "Migration not found", because the route looked only at
    the in-memory registry before it ever asked the record."""

    def test_it_is_removed(self, api, seed, registry, monkeypatch):
        admin = seed.user('root', role='admin')
        forgotten = []
        _install_fake_record_keeper(
            monkeypatch, lambda mid: (forgotten.append(mid), {'forgotten': True})[1],
            recorded=[{'id': 'fromdb', 'status': 'failed'}])

        resp = api.as_user(admin).delete('/api/xhm/migrations/fromdb')

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert forgotten == ['fromdb']

    def test_a_refusal_is_an_error_with_the_reason(self, api, seed, registry, monkeypatch):
        admin = seed.user('root', role='admin')
        _install_fake_record_keeper(
            monkeypatch, lambda mid: {'forgotten': False, 'error': 'still has 2 resource(s)'},
            recorded=[{'id': 'fromdb', 'status': 'failed'}])

        resp = api.as_user(admin).delete('/api/xhm/migrations/fromdb')

        assert resp.status_code == 409
        assert resp.get_json()['error'] == 'still has 2 resource(s)'

    def test_an_id_neither_side_knows_is_still_a_404(self, api, seed, registry, monkeypatch):
        admin = seed.user('root', role='admin')
        _install_fake_record_keeper(monkeypatch, lambda mid: {'forgotten': True},
                                   recorded=[{'id': 'other', 'status': 'failed'}])

        assert api.as_user(admin).delete('/api/xhm/migrations/nope').status_code == 404


class TestTheLogStaysUntilTheRecordHasGone:
    """The log of a migration lives only in this process. Dismissing used to drop the
    in-memory entry first and ask the durable record afterwards, so a record that stayed
    brought the row back on the next load — without the log it had a moment ago."""

    def test_a_refused_record_keeps_the_entry_and_its_log(self, api, seed, registry,
                                                          monkeypatch):
        admin = seed.user('root', role='admin')
        registry['mig1'] = _Task('mig1', status='failed')
        _install_fake_record_keeper(
            monkeypatch, lambda mid: {'forgotten': False, 'error': 'record busy'},
            recorded=[{'id': 'mig1', 'status': 'failed'}])

        resp = api.as_user(admin).delete('/api/xhm/migrations/mig1')

        assert resp.status_code == 409
        assert resp.get_json()['kept'] == [{'id': 'mig1', 'reason': 'record busy'}]
        assert 'mig1' in registry

    def test_a_record_that_went_takes_the_entry_with_it(self, api, seed, registry,
                                                        monkeypatch):
        admin = seed.user('root', role='admin')
        registry['mig1'] = _Task('mig1', status='failed')
        _install_fake_record_keeper(monkeypatch, lambda mid: {'forgotten': True},
                                    recorded=[{'id': 'mig1', 'status': 'failed'}])

        resp = api.as_user(admin).delete('/api/xhm/migrations/mig1')

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()['removed'] == ['mig1']
        assert 'mig1' not in registry

    def test_clearing_finished_keeps_the_entries_whose_record_stayed(self, api, seed,
                                                                     registry, monkeypatch):
        admin = seed.user('root', role='admin')
        registry['kept1'] = _Task('kept1', status='failed')
        registry['gone1'] = _Task('gone1', status='completed')
        registry['live'] = _Task('live', status='running')
        _install_fake_record_keeper(
            monkeypatch,
            lambda mid: ({'forgotten': True} if mid != 'kept1'
                         else {'forgotten': False, 'error': 'record busy'}),
            recorded=[{'id': 'kept1', 'status': 'failed'},
                      {'id': 'gone1', 'status': 'completed'}])

        resp = api.as_user(admin).delete('/api/xhm/migrations')

        body = resp.get_json()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert body['removed'] == ['gone1']
        assert body['kept'] == [{'id': 'kept1', 'reason': 'record busy'}]
        assert sorted(registry) == ['kept1', 'live']

    def test_a_record_that_could_not_be_asked_counts_as_kept(self, monkeypatch):
        """An exception is not an answer. Reading it as "gone" was the other way the
        in-memory entry disappeared while the record stayed behind."""
        def broken(mid):
            raise OSError('database locked')

        _install_fake_record_keeper(monkeypatch, broken,
                                    recorded=[{'id': 'mig1', 'status': 'failed'}])

        answers = list(xhm_api._forget_recorded('mig1'))

        assert [mid for mid, _ in answers] == ['mig1']
        assert answers[0][1]

    def test_the_selected_log_is_closed_only_for_a_row_that_went(self):
        """Read out of the source: the list lives in a component this suite cannot mount."""
        with open(os.path.join(REPO, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
            source = fh.read()
        start = source.index('const dismissXhmMigrations = async')
        handler = source[start:source.index('const xhmClusterLabel', start)]

        assert 'if (!mid) setXhmSelectedMigration(null)' not in handler
        assert "(data.removed || []).includes(xhmSelectedMigration)" in handler


def test_removing_from_the_list_is_asked_before_it_happens():
    """Read out of the source: the list lives in a component this suite cannot mount.
    Both the X on a row and "clear finished" open a confirmation; neither deletes."""
    with open(os.path.join(REPO, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
        source = fh.read()

    assert 'dismissXhmMigrations(m.id)' not in source
    assert 'onClick={() => dismissXhmMigrations()}' not in source
    assert source.count('setXhmDismissAsk(') >= 3

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


def _install_fake_record_keeper(monkeypatch, forget, recorded=(), may_see=lambda row: True):
    """Stand in for the fork's Hyper-V module, which this patch does not depend on."""
    import sys
    import types

    monkeypatch.setattr(xhm_api, '_may_dismiss_record', may_see)

    module = types.ModuleType('pegaprox.core.hyperv_xhm')
    module.forget_recorded_migration = forget
    module.recorded_migrations = lambda known: list(recorded)
    monkeypatch.setitem(sys.modules, 'pegaprox.core.hyperv_xhm', module)


class TestARunThatCompletedWithErrors:
    """Read out of the source: the list lives in a component this suite cannot mount.

    A Hyper-V import whose driver injection failed ends with the VM built but not as asked.
    The list showed the status in grey text on the fallback colour and left the last phase
    unticked, or - once the run said "completed" - green, which hid the failure."""

    def test_the_badge_dot_and_bar_are_yellow_not_green(self):
        rows = _migration_list(_dashboard())
        assert "const withErrors = m.status === 'completed_with_errors';" in rows
        assert "withErrors ? 'bg-yellow-400'" in rows
        assert "withErrors ? 'bg-yellow-500/20 text-yellow-400'" in rows

    def test_the_badge_says_what_happened_in_words(self):
        rows = _migration_list(_dashboard())
        assert "t('xhmCompletedWithErrors')" in rows

    def test_the_last_phase_is_reached_but_marked(self):
        rows = _migration_list(_dashboard())
        assert "d.status === 'completed_with_errors')));" in rows
        assert "warned ? '!'" in rows

    def test_the_label_exists_in_both_languages(self):
        with open(os.path.join(REPO, 'web', 'src', 'translations.js'), encoding='utf-8') as fh:
            assert fh.read().count('xhmCompletedWithErrors:') == 2
