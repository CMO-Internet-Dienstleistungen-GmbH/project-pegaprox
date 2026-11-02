# Up to N calls per Hyper-V host at once (docs/adr/0006).
#
# One session per host put every user of a host into one queue: four preflights on four
# VMs took as long as the four in a row. The pool spreads calls over N sessions. What it
# must not give up on the way is what the single session guaranteed -- never more shells
# on the host than configured, one broken shell not taking the others with it, a caller
# that cannot get a session hearing so instead of waiting forever, and two inspections of
# one VM never mounting the same disks at once.
#
# The concurrency tests synchronise on events rather than sleeping, so they neither pass
# by luck on a fast machine nor fail on a slow one.

import sys
import threading
import time

import pytest

from pegaprox.core import hyperv, hyperv_client as hc, hyperv_tasks
from pegaprox.core.hyperv_errors import HyperVError, KIND_BUSY, KIND_REFUSED

FAKE_PASSWORD = 'fixture-' + 'value-' + 'not-a-real-credential'
GUID_A = '11111111-1111-1111-1111-111111111111'
GUID_B = '22222222-2222-2222-2222-222222222222'
# How long a test waits for something that should happen at once. Only ever reached when
# the code under test is broken, so it can be generous.
PATIENCE = 5


def _conn():
    return hc.HyperVConnection(host='probe-host.example', username='probe-account',
                               password=FAKE_PASSWORD)


def _wait_until(condition, timeout=PATIENCE):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class _Gate:
    """Counts who is inside a call and holds them there until released."""

    def __init__(self):
        self.lock = threading.Lock()
        self.inside = 0
        self.peak = 0
        self.entered = 0
        self.release = threading.Event()

    def hold(self):
        with self.lock:
            self.inside += 1
            self.entered += 1
            self.peak = max(self.peak, self.inside)
        try:
            assert self.release.wait(PATIENCE), 'the test never released the call'
        finally:
            with self.lock:
                self.inside -= 1


class _HeldSession:
    """A session that waits at the gate, like a script the host takes its time over."""

    def __init__(self, gate):
        self.gate = gate
        self.closed = 0

    def run_json(self, script, **parameters):
        hyperv_tasks.mark_running()
        self.gate.hold()
        return {'script': script}

    def run_action(self, script, description, **parameters):
        hyperv_tasks.mark_running()
        self.gate.hold()
        return {'script': script}

    def close(self):
        self.closed += 1


def _pool(gate, max_sessions=4, wait_seconds=PATIENCE):
    created = []

    def factory(_connection):
        session = _HeldSession(gate)
        created.append(session)
        return session

    pool = hc.PooledHyperVClient(_conn(), max_sessions=max_sessions,
                                 wait_seconds=wait_seconds, session_factory=factory)
    return pool, created


def _start(target, *args):
    errors = []

    def run():
        try:
            target(*args)
        except BaseException as exc:                      # noqa: BLE001 - reported below
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


class TestNCallsRunAtOnce:
    def test_n_calls_run_together_and_the_next_one_waits(self):
        gate = _Gate()
        pool, created = _pool(gate, max_sessions=3)

        threads = [_start(pool.run_json, 'Get-VM') for _ in range(4)]
        assert _wait_until(lambda: gate.inside == 3), 'three sessions should be in use at once'
        # Give the fourth every chance to get in if nothing stops it.
        time.sleep(0.05)
        assert gate.inside == 3, 'the fourth call must wait for a free session'

        gate.release.set()
        for thread, errors in threads:
            thread.join(PATIENCE)
            assert not errors
        assert gate.peak == 3
        assert gate.entered == 4

    def test_the_host_never_sees_more_sessions_than_configured(self):
        # The shell count on the host is the acceptance criterion; the sessions created
        # here are the shells.
        gate = _Gate()
        gate.release.set()
        pool, created = _pool(gate, max_sessions=2)

        threads = [_start(pool.run_json, 'Get-VM') for _ in range(10)]
        for thread, _errors in threads:
            thread.join(PATIENCE)
        assert len(created) <= 2

    def test_a_host_with_one_caller_at_a_time_keeps_one_session(self):
        gate = _Gate()
        gate.release.set()
        pool, created = _pool(gate, max_sessions=4)

        for _ in range(5):
            pool.run_json('Get-VM')
        assert len(created) == 1

    def test_a_call_waiting_for_a_session_shows_as_waiting(self):
        # In the task bar, N calls read "running" and the rest "waiting" -- the difference
        # between a busy host and a hung one.
        gate = _Gate()
        pool, _created = _pool(gate, max_sessions=1)

        def tracked():
            with hyperv_tasks.track('hv-pool-host', 'hv_detail', GUID_A):
                pool.run_json('Get-VM')

        first = _start(tracked)
        assert _wait_until(lambda: gate.inside == 1)
        second = _start(tracked)
        assert _wait_until(lambda: len(hyperv_tasks.tasks_for('hv-pool-host')) == 2)

        statuses = sorted(t.status for t in hyperv_tasks.tasks_for('hv-pool-host'))
        assert statuses == [hyperv_tasks.STATUS_QUEUED, hyperv_tasks.STATUS_RUNNING]

        gate.release.set()
        for thread, _errors in (first, second):
            thread.join(PATIENCE)
        hyperv_tasks.forget_host('hv-pool-host')


class TestWaitingHasAnEnd:
    def test_a_caller_that_gets_no_session_is_told_the_host_is_busy(self):
        gate = _Gate()
        pool, _created = _pool(gate, max_sessions=1, wait_seconds=0.05)
        holder = _start(pool.run_json, 'Get-VM')
        assert _wait_until(lambda: gate.inside == 1)

        with pytest.raises(HyperVError) as caught:
            pool.run_json('Get-VM')

        assert caught.value.kind == KIND_BUSY
        gate.release.set()
        holder[0].join(PATIENCE)

    def test_the_wait_defaults_to_the_read_timeout(self):
        pool = hc.PooledHyperVClient(_conn())
        assert pool._wait_seconds == _conn().read_timeout

    def test_a_refused_script_is_refused_without_waiting_for_a_session(self):
        # A script that may not go out must not queue behind N others to be told so.
        gate = _Gate()
        pool, _created = _pool(gate, max_sessions=1, wait_seconds=PATIENCE)
        holder = _start(pool.run_json, 'Get-VM')
        assert _wait_until(lambda: gate.inside == 1)

        started = time.monotonic()
        with pytest.raises(HyperVError) as caught:
            pool.run_json('Remove-VM -Name x')
        assert caught.value.kind == KIND_REFUSED
        assert time.monotonic() - started < 1

        gate.release.set()
        holder[0].join(PATIENCE)

    def test_a_session_that_cannot_be_built_gives_its_slot_back(self):
        attempts = []

        def factory(_connection):
            attempts.append(1)
            if len(attempts) == 1:
                raise HyperVError('handshake failed', kind='unreachable')
            gate = _Gate()
            gate.release.set()
            return _HeldSession(gate)

        pool = hc.PooledHyperVClient(_conn(), max_sessions=1, wait_seconds=0.05,
                                     session_factory=factory)
        with pytest.raises(HyperVError):
            pool.run_json('Get-VM')
        # With the slot lost, this would be "busy" instead of an answer.
        assert pool.run_json('Get-VM') == {'script': 'Get-VM'}


class _StubPowerShell:
    def __init__(self, output):
        self._output = output
        self.had_errors = False
        self.streams = type('S', (), {'error': []})()

    def add_script(self, _script):
        return self

    def add_parameter(self, *_args):
        return self

    def invoke(self):
        return self._output


class TestOneBrokenSessionStaysOne:
    def test_a_transport_failure_drops_only_its_own_session(self, monkeypatch):
        broken, healthy = object(), object()
        sessions = [hc.PsrpHyperVClient(_conn()), hc.PsrpHyperVClient(_conn())]
        sessions[0]._pool, sessions[0]._wsman = broken, object()
        sessions[1]._pool, sessions[1]._wsman = healthy, object()
        healthy_wsman = sessions[1]._wsman

        def powershell(pool):
            if pool is broken:
                raise OSError('connection reset by peer')
            return _StubPowerShell(['{"ok": true}'])

        monkeypatch.setitem(sys.modules, 'pypsrp.powershell',
                            type('M', (), {'PowerShell': powershell}))
        handed_out = iter(sessions)
        pool = hc.PooledHyperVClient(_conn(), max_sessions=2, wait_seconds=0.05,
                                     session_factory=lambda _c: next(handed_out))
        # Both sessions exist, the broken one is handed out first.
        pool._sessions = list(sessions)
        pool._idle = [sessions[1], sessions[0]]

        with pytest.raises(HyperVError):
            pool.run_json('Get-VM')

        assert sessions[0]._pool is None, 'the broken shell is thrown away'
        assert sessions[1]._pool is healthy, 'the healthy one is left alone'
        assert sessions[1]._wsman is healthy_wsman
        # Both slots are free again: the failure did not leak one.
        assert pool._slots.acquire(timeout=0.05) and pool._slots.acquire(timeout=0.05)
        pool._slots.release()
        pool._slots.release()
        pool._idle.remove(sessions[0])
        assert pool.run_json('Get-VM') == {'ok': True}


class TestClosing:
    def test_close_closes_every_session(self):
        gate = _Gate()
        pool, created = _pool(gate, max_sessions=2)
        threads = [_start(pool.run_json, 'Get-VM') for _ in range(2)]
        assert _wait_until(lambda: gate.inside == 2)
        gate.release.set()
        for thread, _errors in threads:
            thread.join(PATIENCE)

        pool.close()

        assert [session.closed for session in created] == [1, 1]


class TestTheSetting:
    @pytest.mark.parametrize('value, expected', [
        (None, 4), ('', 4), (1, 1), (8, 8), ('6', 6), (4.0, 4)])
    def test_accepted_values(self, value, expected):
        assert hc.parse_max_sessions(value) == expected

    @pytest.mark.parametrize('value', [0, 9, -1, '0', '12', 'four', 4.5, True, [], {}])
    def test_refused_values(self, value):
        with pytest.raises(ValueError):
            hc.parse_max_sessions(value)

    def test_the_default_is_four(self):
        assert hc.DEFAULT_MAX_SESSIONS == 4
        assert hc.PooledHyperVClient(_conn()).max_sessions == 4


class _HeldClient:
    """A manager's client whose calls wait at the gate."""

    def __init__(self, gate):
        self.gate = gate

    def run_json(self, script, **parameters):
        self.gate.hold()
        return {}

    def run_action(self, script, description, **parameters):
        self.gate.hold()
        return {}

    def close(self):
        pass


class TestOneVmIsChangedByOneCallAtATime:
    def _manager(self, gate):
        return hyperv.HyperVManager('hv-lock-host', _HeldClient(gate), host='probe-host.example')

    def test_two_inspections_of_one_vm_never_overlap(self):
        # Two Mount-VHD on one file lock each other out, and a second inspection while the
        # first holds the disks reports them as attached.
        gate = _Gate()
        manager = self._manager(gate)
        threads = [_start(manager.inspect_disks, GUID_A),
                   _start(manager.inspect_disks, GUID_A.upper())]
        assert _wait_until(lambda: gate.inside == 1)
        time.sleep(0.05)
        assert gate.inside == 1

        gate.release.set()
        for thread, errors in threads:
            thread.join(PATIENCE)
            assert not errors
        assert gate.peak == 1
        assert gate.entered == 2
        hyperv_tasks.forget_host('hv-lock-host')

    def test_inspections_of_two_vms_run_together(self):
        gate = _Gate()
        manager = self._manager(gate)
        threads = [_start(manager.inspect_disks, GUID_A), _start(manager.inspect_disks, GUID_B)]
        assert _wait_until(lambda: gate.inside == 2)

        gate.release.set()
        for thread, _errors in threads:
            thread.join(PATIENCE)
        hyperv_tasks.forget_host('hv-lock-host')

    def test_reads_of_one_vm_run_together(self):
        gate = _Gate()
        manager = self._manager(gate)
        threads = [_start(manager.get_vm_state, GUID_A) for _ in range(2)]
        assert _wait_until(lambda: gate.inside == 2)

        gate.release.set()
        for thread, _errors in threads:
            thread.join(PATIENCE)
        hyperv_tasks.forget_host('hv-lock-host')


CONFIG = {'name': 'Hyper-V site A', 'host': 'probe-host.example', 'user': 'probe-account',
          'pass': FAKE_PASSWORD}


class _ReachableManager:
    def host_facts(self):
        return {'ComputerName': 'probe', 'Version': '10.0'}

    def verify_properties(self):
        return {}

    def close(self):
        pass


class TestTheHostSetting:
    def test_concurrent_connects_build_one_manager(self, db, monkeypatch):
        from pegaprox.core import hyperv_cluster

        built = []
        building = threading.Event()
        proceed = threading.Event()

        def build(_self):
            built.append(1)
            building.set()
            assert proceed.wait(PATIENCE)
            return _ReachableManager()

        monkeypatch.setattr(hyperv_cluster.HyperVClusterManager, '_build_manager', build)
        cluster = hyperv_cluster.HyperVClusterManager('hv-connect', CONFIG)

        threads = [_start(cluster.connect) for _ in range(3)]
        assert building.wait(PATIENCE)
        time.sleep(0.05)
        proceed.set()
        for thread, errors in threads:
            thread.join(PATIENCE)
            assert not errors

        assert len(built) == 1

    def test_the_configured_count_reaches_the_pool(self, db):
        from pegaprox.core import hyperv_cluster

        cluster = hyperv_cluster.HyperVClusterManager('hv-a', {**CONFIG, 'max_sessions': 6})
        assert cluster._build_manager()._client.max_sessions == 6

    def test_a_host_saved_without_the_setting_gets_four(self, db):
        from pegaprox.core import hyperv_cluster

        cluster = hyperv_cluster.HyperVClusterManager('hv-a', CONFIG)
        assert cluster._build_manager()._client.max_sessions == 4

    def test_an_impossible_stored_count_does_not_take_the_host_offline(self, db):
        from pegaprox.core import hyperv_cluster

        cluster = hyperv_cluster.HyperVClusterManager('hv-a', {**CONFIG, 'max_sessions': 99})
        assert cluster.config.max_sessions == 4


OLD_HOSTS_TABLE = '''
    CREATE TABLE hyperv_hosts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
        username TEXT NOT NULL DEFAULT '', pass_encrypted TEXT DEFAULT '',
        winrm_port INTEGER DEFAULT 5986, verify_certificate INTEGER DEFAULT 1,
        iso_library_paths TEXT DEFAULT '[]', smb_share_map TEXT DEFAULT '{}',
        smb_domain TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)
'''


def _plain(value):
    return value


class TestTheStoredSetting:
    def test_a_host_registered_before_the_column_gets_four(self, db):
        from pegaprox.core import hyperv_db

        conn = db.conn
        conn.execute('DROP TABLE IF EXISTS hyperv_hosts')
        conn.execute(OLD_HOSTS_TABLE)
        conn.execute("INSERT INTO hyperv_hosts (id, name, host, created_at, updated_at) "
                     "VALUES ('h1', 'source', 'hv.invalid', 0, 0)")
        conn.commit()

        hyperv_db.ensure_schema(conn.cursor())
        conn.commit()

        assert hyperv_db.load_host(conn, _plain, 'h1')['max_sessions'] == 4

    def test_the_setting_survives_a_round_trip(self, db):
        from pegaprox.core import hyperv_db

        hyperv_db.save_host(db.conn, _plain, 'h2', {'name': 'n', 'host': 'hv.invalid',
                                                    'user': 'svc', 'max_sessions': 7})
        assert hyperv_db.load_host(db.conn, _plain, 'h2')['max_sessions'] == 7

    def test_a_new_host_without_the_setting_gets_four(self, db):
        from pegaprox.core import hyperv_db

        hyperv_db.save_host(db.conn, _plain, 'h3', {'name': 'n', 'host': 'hv.invalid',
                                                    'user': 'svc'})
        assert hyperv_db.load_host(db.conn, _plain, 'h3')['max_sessions'] == 4

    def test_a_count_edited_into_the_table_by_hand_reads_as_four(self, db):
        from pegaprox.core import hyperv_db

        hyperv_db.save_host(db.conn, _plain, 'h4', {'name': 'n', 'host': 'hv.invalid',
                                                    'user': 'svc'})
        db.conn.execute("UPDATE hyperv_hosts SET max_sessions = 40 WHERE id = 'h4'")
        assert hyperv_db.load_host(db.conn, _plain, 'h4')['max_sessions'] == 4

    def test_an_impossible_count_is_not_stored(self, db):
        from pegaprox.core import hyperv_db

        with pytest.raises(ValueError):
            hyperv_db.save_host(db.conn, _plain, 'h5', {'name': 'n', 'host': 'hv.invalid',
                                                        'user': 'svc', 'max_sessions': 0})


HOST = 'hv_pool'


@pytest.fixture
def registered_host(api, seed, monkeypatch):
    from pegaprox.core import hyperv_cluster, hyperv_db

    hyperv_db.save_host(seed.db.conn, seed.db._encrypt, HOST, {
        'name': 'Lab Hyper-V', 'host': 'hyperv.example', 'user': 'svc',
        'pass': FAKE_PASSWORD, 'max_sessions': 2,
    })
    connected_with = []

    class _TestConnection:
        def __init__(self):
            self.stopped = threading.Event()

        def stop(self):
            self.stopped.set()

    test_connections = []

    def connect(host_id, data):
        connected_with.append(dict(data))
        test_connections.append(_TestConnection())
        return test_connections[-1], None

    monkeypatch.setattr(hyperv_cluster, 'connect_hyperv_source', connect)
    monkeypatch.setattr(hyperv_cluster, 'register_hyperv_source',
                        lambda host_id, record, managers: managers.__setitem__(host_id, object()))
    return {'connected_with': connected_with, 'test_connections': test_connections,
            'stopped_class': _TestConnection}


class TestTheForm:
    def _as_admin(self, api, seed):
        return api.as_user(seed.user('root', role='admin'))

    @pytest.mark.parametrize('value', [0, 9, 'many'])
    def test_an_impossible_count_is_refused_before_the_host_is_asked(
            self, api, seed, registered_host, value):
        response = self._as_admin(api, seed).put(
            f'/api/hyperv/hosts/{HOST}',
            json={'name': 'Lab Hyper-V', 'host': 'hyperv.example', 'max_sessions': value})

        assert response.status_code == 400
        assert 'between 1 and 8' in response.get_json()['error'] or \
            'whole number' in response.get_json()['error']
        assert registered_host['connected_with'] == []

    def test_creating_a_host_with_an_impossible_count_is_refused(self, api, seed,
                                                                 registered_host):
        response = self._as_admin(api, seed).post(
            '/api/hyperv/hosts',
            json={'name': 'New', 'host': 'hyperv-new.example', 'max_sessions': 20})

        assert response.status_code == 400
        assert registered_host['connected_with'] == []

    def test_the_listing_carries_the_count(self, api, seed, registered_host):
        hosts = self._as_admin(api, seed).get('/api/hyperv/hosts').get_json()['hosts']
        assert [h['max_sessions'] for h in hosts if h['id'] == HOST] == [2]

    def test_saving_stores_the_count_and_closes_the_test_connection(self, api, seed,
                                                                    registered_host):
        from pegaprox.core import hyperv_db

        response = self._as_admin(api, seed).put(
            f'/api/hyperv/hosts/{HOST}',
            json={'name': 'Lab Hyper-V', 'host': 'hyperv.example', 'max_sessions': 5})

        assert response.status_code == 200, response.get_data(as_text=True)[:400]
        assert hyperv_db.load_host(seed.db.conn, seed.db._decrypt, HOST)['max_sessions'] == 5
        # Left open, every save of the form would leave up to N shells on the host.
        assert registered_host['test_connections'][-1].stopped.is_set()

    def test_saving_closes_the_source_it_replaces(self, api, seed, registered_host):
        from pegaprox.globals import cluster_managers

        previous = registered_host['stopped_class']()
        cluster_managers[HOST] = previous
        try:
            response = self._as_admin(api, seed).put(
                f'/api/hyperv/hosts/{HOST}',
                json={'name': 'Lab Hyper-V', 'host': 'hyperv.example'})
            assert response.status_code == 200, response.get_data(as_text=True)[:400]
            assert previous.stopped.wait(PATIENCE)
        finally:
            cluster_managers.pop(HOST, None)
