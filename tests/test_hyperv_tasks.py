# Fork patch: the calls PegaProx makes to a Hyper-V host, shown as tasks.
#
# Before this, `get_tasks` on a Hyper-V host returned an empty list, and a call waiting for
# the host's one PowerShell session looked exactly like a call that had hung. What has to
# hold, and what each test here fails on if it is lost:
#
#   A call is visible from the moment it wants a session: queued, then running once the
#   transport holds the session, then OK or error.
#
#   The rows are shaped like every other provider's. `starttime` is Unix seconds -- an ISO
#   string makes the task bar's sort and its auto-expand check NaN (#738 upstream) -- and
#   `vmid` is the synthetic VMID the scoped task route filters on.
#
#   Nothing that identifies the environment reaches a row or the task log: the error text is
#   the transport's already-redacted message, and an error from anywhere else keeps only its
#   type.

import sys
import threading
import time

import pytest

from pegaprox.core import hyperv_client as hc
from pegaprox.core import hyperv_cluster
from pegaprox.core import hyperv_tasks
from pegaprox.core.hyperv import HyperVManager
from pegaprox.core.hyperv_errors import HyperVError

HOST = 'hyperv-tasks'
GUID = '11111111-1111-1111-1111-111111111111'
FAKE_PASSWORD = 'fixture-' + 'value-' + 'not-a-real-credential'
FAKE_HOST = 'probe-host.example'
FAKE_ACCOUNT = 'probe-account'


@pytest.fixture(autouse=True)
def _empty_register():
    """The register is process-global; every test starts from nothing."""
    with hyperv_tasks._lock:
        hyperv_tasks._tasks.clear()
    yield
    with hyperv_tasks._lock:
        hyperv_tasks._tasks.clear()


def _only_task(host=HOST):
    tasks = hyperv_tasks.tasks_for(host)
    assert len(tasks) == 1, tasks
    return tasks[0]


class TestTheLifeOfOneCall:
    def test_a_call_is_queued_until_the_transport_holds_a_session(self):
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            assert _only_task().status == hyperv_tasks.STATUS_QUEUED
            hyperv_tasks.mark_running()
            task = _only_task()
            assert task.status == hyperv_tasks.STATUS_RUNNING
            assert task.started_at is not None

    def test_a_call_that_returns_ends_ok(self):
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            hyperv_tasks.mark_running()
        task = _only_task()
        assert task.status == hyperv_tasks.STATUS_OK
        assert task.ended_at is not None
        assert task.error == ''

    def test_a_call_that_raises_ends_as_error_and_the_error_still_propagates(self):
        with pytest.raises(HyperVError):
            with hyperv_tasks.track(HOST, 'hv_detail', GUID):
                raise HyperVError('The VM is locked by another operation.', kind='unknown')
        task = _only_task()
        assert task.status == hyperv_tasks.STATUS_ERROR
        assert task.error == 'The VM is locked by another operation.'

    def test_an_error_from_outside_the_transport_keeps_only_its_type(self):
        # Its text promised no redaction, and the task bar is shown to everyone who can
        # see the host.
        with pytest.raises(RuntimeError):
            with hyperv_tasks.track(HOST, 'hv_detail', GUID):
                raise RuntimeError(f'failed for {FAKE_ACCOUNT} on {FAKE_HOST}')
        task = _only_task()
        assert FAKE_HOST not in task.error
        assert FAKE_ACCOUNT not in task.error
        assert 'RuntimeError' in task.error

    def test_a_nested_call_is_one_task_not_two(self):
        with hyperv_tasks.track(HOST, 'hv_safety_check', GUID):
            with hyperv_tasks.track(HOST, 'hv_state', GUID):
                pass
            with hyperv_tasks.track(HOST, 'hv_merge_state', GUID):
                pass
        assert _only_task().task_type == 'hv_safety_check'

    def test_mark_running_outside_a_call_changes_nothing(self):
        hyperv_tasks.mark_running()
        assert hyperv_tasks.tasks_for(HOST) == []

    def test_a_call_without_a_request_is_filed_under_the_system(self):
        with hyperv_tasks.track(HOST, 'hv_inventory'):
            pass
        assert _only_task().user == hyperv_tasks.SYSTEM_USER


class TestWhatTheRegisterKeeps:
    def test_a_finished_call_is_dropped_after_its_retention(self, monkeypatch):
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            pass
        later = time.time() + hyperv_tasks.FINISHED_RETENTION_SECONDS + 1
        monkeypatch.setattr(hyperv_tasks.time, 'time', lambda: later)
        assert hyperv_tasks.tasks_for(HOST) == []

    def test_a_call_still_waiting_is_never_dropped_for_age(self, monkeypatch):
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            later = time.time() + hyperv_tasks.FINISHED_RETENTION_SECONDS * 10
            monkeypatch.setattr(hyperv_tasks.time, 'time', lambda: later)
            assert len(hyperv_tasks.tasks_for(HOST)) == 1

    def test_the_cap_drops_the_oldest_finished_calls_first(self, monkeypatch):
        monkeypatch.setattr(hyperv_tasks, 'MAX_TASKS_PER_HOST', 3)
        with hyperv_tasks.track(HOST, 'hv_inspect', GUID):
            for _ in range(5):
                # Each on its own context, the way separate requests arrive; nested in the
                # open one they would fold into it.
                threading.Thread(target=self._one_call).start()
                time.sleep(0.01)
            time.sleep(0.05)
            tasks = hyperv_tasks.tasks_for(HOST)
            assert len(tasks) == 3
            # The call still open is the one somebody is looking at, so it survives.
            assert any(t.task_type == 'hv_inspect' and not t.finished for t in tasks)

    @staticmethod
    def _one_call():
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            pass

    def test_hosts_do_not_see_each_others_calls(self):
        with hyperv_tasks.track('host-a', 'hv_detail', GUID):
            pass
        assert hyperv_tasks.tasks_for('host-b') == []

    def test_a_removed_host_takes_its_calls_with_it(self):
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            pass
        hyperv_tasks.forget_host(HOST)
        assert hyperv_tasks.tasks_for(HOST) == []


class _Client:
    """Answers every script and records the task that was current while it ran."""

    def __init__(self):
        self.seen = []

    def run_json(self, script, **parameters):
        hyperv_tasks.mark_running()
        self.seen.append(hyperv_tasks._current.get())
        return {'Id': GUID, 'State': 'Off', 'CheckpointCount': 0, 'PrimaryStatus': 2}

    def run_action(self, script, description, **parameters):
        return self.run_json(script)

    def close(self):
        pass


class TestTheManagerNamesEveryCall:
    def test_a_read_is_tracked_under_its_type_and_vm(self):
        manager = HyperVManager(HOST, _Client())
        manager.get_vm_state(GUID)
        task = _only_task()
        assert (task.task_type, task.vm_guid, task.status) == ('hv_state', GUID, 'OK')

    def test_an_action_is_tracked_too(self):
        manager = HyperVManager(HOST, _Client())
        manager.inspect_disks(GUID)
        assert _only_task().task_type == 'hv_inspect'

    def test_the_safety_gate_is_one_task_however_many_questions_it_asks(self):
        client = _Client()
        manager = HyperVManager(HOST, client)
        manager.disks_are_safe_to_read(GUID)
        assert len(client.seen) == 2
        assert client.seen[0] is client.seen[1]
        assert _only_task().task_type == 'hv_safety_check'


def _conn():
    return hc.HyperVConnection(host=FAKE_HOST, username=FAKE_ACCOUNT, password=FAKE_PASSWORD)


class _SlowShell:
    def __init__(self, release):
        self._release = release
        self.had_errors = False

    def add_script(self, script):
        pass

    def add_parameter(self, name, value):
        pass

    def invoke(self):
        self._release.wait(5)
        return ['{}']


class TestTheTransportSaysWhenTheWaitEnds:
    def test_a_call_behind_another_stays_queued_until_the_session_is_free(self, monkeypatch):
        release = threading.Event()
        client = hc.PsrpHyperVClient(_conn())
        monkeypatch.setattr(client, '_ensure_pool', lambda: object())
        monkeypatch.setitem(sys.modules, 'pypsrp.powershell',
                            type('M', (), {'PowerShell': lambda _pool: _SlowShell(release)}))
        manager = HyperVManager(HOST, client)

        first = threading.Thread(target=manager.get_vm_state, args=(GUID,))
        second = threading.Thread(target=manager.list_vms)
        first.start()
        time.sleep(0.05)
        second.start()
        time.sleep(0.05)

        by_type = {t.task_type: t.status for t in hyperv_tasks.tasks_for(HOST)}
        assert by_type == {'hv_state': 'running', 'hv_inventory': 'queued'}

        release.set()
        first.join(5)
        second.join(5)
        assert {t.status for t in hyperv_tasks.tasks_for(HOST)} == {'OK'}


class _Manager:
    """Just enough of a HyperVManager for the adapter to connect."""

    def host_facts(self):
        return {'os_caption': 'Windows Server 2022', 'powershell_version': '5.1'}

    def verify_properties(self):
        return {'complete': True, 'missing': {}, 'inspected_vm': 'x'}

    def close(self):
        pass


CONFIG = {'name': 'Hyper-V site A', 'host': FAKE_HOST, 'user': FAKE_ACCOUNT,
          'pass': FAKE_PASSWORD, 'port': 5986}


def _adapter():
    cluster = hyperv_cluster.HyperVClusterManager(HOST, CONFIG, manager=_Manager())
    cluster.connect()
    return cluster


class TestTheRowsTheTaskBarReads:
    def test_a_row_carries_what_the_task_bar_renders(self, db):
        adapter = _adapter()
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            hyperv_tasks.mark_running()
            row = adapter.get_tasks()[0]
        assert row['type'] == 'hv_detail'
        assert row['status'] == 'running'
        assert row['node'] == 'Hyper-V site A'
        assert row['upid'].startswith('hvq:')
        assert row['cancellable'] is False

    def test_times_are_unix_seconds_not_iso_strings(self, db):
        adapter = _adapter()
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            pass
        row = adapter.get_tasks()[0]
        assert isinstance(row['starttime'], int)
        assert isinstance(row['endtime'], int)
        assert abs(row['starttime'] - time.time()) < 60

    def test_the_vmid_is_the_synthetic_one_the_rest_of_pegaprox_uses(self, db):
        adapter = _adapter()
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            pass
        row = adapter.get_tasks()[0]
        assert row['vmid'] == adapter.vmid_for(GUID)
        assert row['id'] == str(row['vmid'])

    def test_a_host_level_call_has_no_vmid(self, db):
        adapter = _adapter()
        with hyperv_tasks.track(HOST, 'hv_inventory'):
            pass
        assert adapter.get_tasks()[0]['vmid'] is None

    def test_a_failed_call_carries_its_error_as_the_exit_status(self, db):
        adapter = _adapter()
        with pytest.raises(HyperVError):
            with hyperv_tasks.track(HOST, 'hv_detail', GUID):
                raise HyperVError('no such VM', kind='unknown')
        row = adapter.get_tasks()[0]
        assert (row['status'], row['exitstatus']) == ('error', 'no such VM')

    def test_the_limit_is_honoured(self, db):
        adapter = _adapter()
        for _ in range(5):
            with hyperv_tasks.track(HOST, 'hv_inventory'):
                pass
        assert len(adapter.get_tasks(limit=2)) == 2


class TestTheTaskLog:
    def test_the_log_is_a_summary_of_the_call(self, db):
        adapter = _adapter()
        with hyperv_tasks.track(HOST, 'hv_inspect', GUID) as task:
            hyperv_tasks.mark_running()
        lines = adapter.get_node_task_log('Hyper-V site A', task.upid)
        text = '\n'.join(lines)
        assert 'hv_inspect' in text
        assert 'Waited for a session' in text
        assert 'Status: OK' in text

    def test_the_log_of_a_failed_call_names_neither_host_nor_account_nor_password(self, db):
        # The transport's error is what reaches the task; this runs it through the real
        # redaction rather than trusting a hand-written message.
        adapter = _adapter()
        message = hc.redact(f'Access denied for {FAKE_ACCOUNT} at {FAKE_HOST} ({FAKE_PASSWORD})',
                            _conn().redactions())
        with pytest.raises(HyperVError):
            with hyperv_tasks.track(HOST, 'hv_detail', GUID) as task:
                raise HyperVError(message, kind='authentication')
        text = '\n'.join(adapter.get_node_task_log('Hyper-V site A', task.upid))
        for secret in (FAKE_ACCOUNT, FAKE_HOST, FAKE_PASSWORD):
            assert secret not in text

    def test_a_call_no_longer_in_the_list_is_said_so_rather_than_a_500(self, db):
        adapter = _adapter()
        assert adapter.get_node_task_log('Hyper-V site A', 'hvq:gone') == [
            'This Hyper-V call is no longer in the list.']


class TestAScopedCallerSeesOnlyTheirVms:
    """The shared task route filters on `vmid`; the Hyper-V rows have to carry one it reads."""

    def test_a_pool_user_sees_the_calls_for_their_vm_only(self, api, seed, db):
        import pegaprox.utils.rbac as rbac

        adapter = _adapter()
        other_guid = '22222222-2222-2222-2222-222222222222'
        mine = adapter.vmid_for(GUID)
        adapter.vmid_for(other_guid)
        for guid in (GUID, other_guid):
            with hyperv_tasks.track(HOST, 'hv_detail', guid):
                pass
        with hyperv_tasks.track(HOST, 'hv_inventory'):
            pass

        seed.tenant('tenant_x', clusters=[HOST])
        user = seed.user('mallory', role='viewer', tenant_id='tenant_x')
        seed.pool(HOST, 'pool_1', 'mallory', ['pool.view', 'vm.view'])
        with rbac._pool_cache_lock:
            rbac._pool_membership_cache[HOST] = {
                'data': {f'{mine}:qemu': 'pool_1'}, 'timestamp': time.time(),
                'refreshing': False}
        api.set_manager(HOST, adapter)

        resp = api.as_user(user).get(f'/api/clusters/{HOST}/tasks')
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert [row['vmid'] for row in resp.get_json()] == [mine]

    def test_an_admin_sees_every_call(self, api, seed, db):
        adapter = _adapter()
        with hyperv_tasks.track(HOST, 'hv_detail', GUID):
            pass
        with hyperv_tasks.track(HOST, 'hv_inventory'):
            pass
        api.set_manager(HOST, adapter)
        admin = seed.user('root', role='admin')
        resp = api.as_user(admin).get(f'/api/clusters/{HOST}/tasks')
        assert len(resp.get_json()) == 2

    def test_the_request_user_is_who_the_call_is_filed_under(self, api, seed, db):
        adapter = _adapter()
        api.set_manager(HOST, adapter)
        admin = seed.user('root', role='admin')
        seen = {}

        def _record():
            seen['user'] = hyperv_tasks.current_user()
            return []

        adapter.get_tasks = lambda limit=50: _record()
        api.as_user(admin).get(f'/api/clusters/{HOST}/tasks')
        assert seen['user'] == 'root'


class TestTheTaskBarNamesEveryCall:
    """Each type `HyperVManager` files a call under has a label, in German and English.

    `t()` returns the key itself for a missing label, so a gap shows as `hvTaskInspect`
    in the bar rather than failing anywhere.
    """

    def test_every_task_type_has_a_label_in_both_languages(self):
        import os
        import re

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, 'pegaprox', 'core', 'hyperv.py'), encoding='utf-8') as fh:
            types = set(re.findall(r"'(hv_[a-z_]+)'", fh.read()))
        with open(os.path.join(repo, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
            dashboard = fh.read()
        with open(os.path.join(repo, 'web', 'src', 'translations.js'), encoding='utf-8') as fh:
            translations = fh.read()

        assert types, 'no task types found in hyperv.py'
        unmapped = sorted(t for t in types if not re.search(rf"'{t}': t\('hvTask\w+'\)", dashboard))
        assert unmapped == []
        keys = set(re.findall(r"'hv_[a-z_]+': t\('(hvTask\w+)'\)", dashboard)) | {'taskStatusQueued'}
        missing = sorted(k for k in keys
                         if len(re.findall(rf'^\s*{k}:', translations, re.MULTILINE)) < 2)
        assert missing == []
