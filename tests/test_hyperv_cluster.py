# Fork issue #15 — the adapter that lets PegaProx hold a Hyper-V host in cluster_managers.
#
# Two things it has to get right, and one it has to survive.
#
# Identity: every VM leaves this layer with an integer VMID, because the API and the
# access-control layer refuse or silently drop anything else. The mapping is durable, so an
# ACL written against VMID 104 still means the same VM after a restart.
#
# Completeness: PegaProx asks every manager the same Proxmox-shaped questions, many without
# a getattr guard. The existing ESXi facade answers only what migration needs, which makes
# the cluster list raise the moment one is registered. This one answers all of them.
#
# Survival: a migration source that is down must show as disconnected, never prevent the
# cluster list from rendering.

import time

import pytest

from pegaprox.core import hyperv_cluster
from pegaprox.core.hyperv import HyperVManager
from pegaprox.core.hyperv_errors import HyperVError, KIND_AUTHORIZATION

GUID_1 = '11111111-1111-1111-1111-111111111111'
GUID_2 = '22222222-2222-2222-2222-222222222222'


@pytest.fixture(autouse=True)
def _forget_the_cached_inventory():
    """The inventory cache is process-global, and this file shares host ids with others."""
    from pegaprox.core import hyperv_inventory
    hyperv_inventory.reset()
    yield
    hyperv_inventory.reset()

CONFIG = {'name': 'Hyper-V site A', 'host': 'probe-host.example', 'user': 'probe-account',
          'pass': 'fixture-' + 'not-a-real-credential', 'port': 5986}


class FakeManager:
    """A HyperVManager stand-in whose answers a test sets directly."""

    def __init__(self, vms=None, details=None, facts=None, properties=None, raises=None):
        self._vms = vms or []
        self._details = details or {}
        self._facts = facts or {'os_caption': 'Windows Server 2022', 'powershell_version': '5.1'}
        self._properties = properties or {'complete': True, 'missing': {}, 'inspected_vm': 'x'}
        self._raises = raises
        self.closed = False
        self.list_vms_calls = 0

    def host_facts(self):
        if self._raises:
            raise self._raises
        return self._facts

    def verify_properties(self):
        return self._properties

    def list_vms(self):
        self.list_vms_calls += 1
        return self._vms

    def get_vm(self, guid):
        if guid not in self._details:
            raise HyperVError(f'no such VM {guid}', kind='unknown')
        return self._details[guid]

    def close(self):
        self.closed = True


def _cluster(db, manager=None, config=None):
    """An adapter wired to a fake manager and the test database."""
    cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', config or CONFIG,
                                                  manager=manager or FakeManager())
    cluster.connect()
    return cluster


def _summary(guid, name='synthetic-vm', state='Off', **extra):
    vm = {'guid': guid, 'name': name, 'state': state, 'generation': 2,
          'cpu_count': 4, 'memory_startup_bytes': 4294967296, 'memory_mb': 4096}
    vm.update(extra)
    return vm


class TestConnecting:
    def test_a_reachable_host_connects(self, db):
        cluster = _cluster(db)
        assert cluster.is_connected
        assert cluster.running
        assert cluster.connection_error == ''

    def test_an_unreachable_host_reports_the_reason_without_raising(self, db):
        manager = FakeManager(raises=HyperVError('Access is denied.', kind=KIND_AUTHORIZATION))
        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', CONFIG, manager=manager)

        assert cluster.connect() is False
        assert not cluster.is_connected
        assert 'Access is denied' in cluster.connection_error
        # The remedy travels with it, so the cluster list can say what to do.
        assert 'Hyper-V Administrators' in cluster.connection_error

    def test_an_unexpected_error_also_leaves_the_manager_usable(self, db):
        # A source being down must never break the page that lists it.
        manager = FakeManager(raises=RuntimeError('something nobody predicted'))
        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', CONFIG, manager=manager)
        assert cluster.connect() is False
        assert cluster.get_nodes()[0]['status'] == 'offline'

    def test_missing_host_properties_are_recorded_not_fatal(self, db):
        # A documentation gap should be a clear warning about this host, not a product
        # that refuses to start.
        manager = FakeManager(properties={'complete': False,
                                          'missing': {'VM': ['Generation']},
                                          'inspected_vm': 'x'})
        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', CONFIG, manager=manager)
        assert cluster.connect() is True
        assert cluster.property_report['missing']['VM'] == ['Generation']

    def test_stopping_closes_the_transport(self, db):
        manager = FakeManager()
        cluster = _cluster(db, manager)
        cluster.stop()
        assert manager.closed
        assert not cluster.is_connected


class TestWhatAVmRowCarries:
    """The shared lists divide by these fields; an absent one is not a blank cell.

    `mem` missing rendered as "NaN MB / 4.0 GB" in the resource table — beside a correct
    maximum, which makes it read as a broken VM rather than a missing figure.
    """

    def test_a_row_carries_the_figures_the_shared_lists_divide_by(self, db):
        cluster = _cluster(db, FakeManager(vms=[_summary(
            GUID_1, state='Running', memory_assigned_bytes=2147483648,
            cpu_usage_percent=25, uptime_seconds=3600)]))
        row = cluster.get_vms()[0]
        assert row['mem'] == 2147483648
        assert row['uptime'] == 3600

    def test_cpu_is_a_fraction_of_the_cores_not_a_percentage(self, db):
        """Proxmox reports `cpu` as 0..1 and the table multiplies by 100 to draw it."""
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1, cpu_usage_percent=25)]))
        assert cluster.get_vms()[0]['cpu'] == 0.25

    def test_a_host_that_reports_nothing_gives_zeroes_rather_than_absent_keys(self, db):
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        row = cluster.get_vms()[0]
        assert row['mem'] == 0 and row['cpu'] == 0 and row['uptime'] == 0


class TestTheInventoryOverviewRow:
    """The overview takes its two counts straight out of `datacenter_status`.

    It reads `nodes.total` and `guests.vms.total`. A node *list* and a top-level `vms`
    key are both truthful and both render as "0 / 0" — a migration source that says it
    holds nothing is the one thing this page may not show.
    """

    def test_nodes_are_a_tally_the_overview_can_read(self, db):
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        nodes = cluster.datacenter_status()['nodes']
        assert nodes == {'online': 1, 'offline': 0, 'total': 1}

    def test_the_guest_counts_sit_where_every_other_cluster_puts_them(self, db):
        from pegaprox.core import hyperv_inventory

        cluster = _cluster(db, FakeManager(vms=[
            _summary(GUID_1, state='Running'), _summary(GUID_2, 'second', state='Off')]))
        # The counts come from the cached inventory: this page is opened for an unrelated
        # cluster and may not spend tens of seconds of a customer's hypervisor on a tally.
        hyperv_inventory.read_now(cluster.id, cluster)
        guests = cluster.datacenter_status()['guests']
        assert guests['vms'] == {'running': 1, 'stopped': 1, 'total': 2}
        assert guests['containers']['total'] == 0

    def test_a_host_nobody_has_read_counts_nothing_rather_than_reading_it(self, db):
        manager = FakeManager(vms=[_summary(GUID_1, state='Running')])
        cluster = _cluster(db, manager)
        assert cluster.datacenter_status()['guests']['vms']['total'] == 0
        assert manager.list_vms_calls == 0

    def test_a_disconnected_host_counts_its_node_as_offline(self, db):
        cluster = _cluster(db, FakeManager(raises=HyperVError('down', kind='unreachable')))
        assert cluster.datacenter_status()['nodes'] == {'online': 0, 'offline': 1, 'total': 1}


class TestIdentity:
    def test_every_listed_vm_has_an_integer_vmid(self, db):
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1), _summary(GUID_2, 'second')]))
        for vm in cluster.get_vms():
            assert isinstance(vm['vmid'], int)

    def test_the_same_vm_keeps_its_vmid_across_listings(self, db):
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        assert cluster.get_vms()[0]['vmid'] == cluster.get_vms()[0]['vmid']

    def test_a_vmid_resolves_back_to_the_hyper_v_guid(self, db):
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        vmid = cluster.get_vms()[0]['vmid']
        assert cluster.guid_for(vmid) == GUID_1

    def test_a_vm_without_an_id_is_left_out_rather_than_listed_broken(self, db):
        # Listing it would offer a row that fails the moment somebody clicks it.
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1), {'name': 'no-id', 'state': 'Off'}]))
        assert len(cluster.get_vms()) == 1

    def test_an_unknown_vmid_is_an_error_not_an_empty_vm(self, db):
        cluster = _cluster(db, FakeManager())
        assert 'error' in cluster.vm_detail(999999)
        assert 'error' in cluster.get_vm_disks_for_export(999999)


def _unguarded_reads(function_name, source_file='pegaprox/api/clusters.py'):
    """Every attribute that function reads off a manager without a getattr guard.

    Derived from the source rather than typed out here. A hand-written list is a snapshot
    of what somebody noticed once: the missing `last_run` got past a hand-written version
    of this test and surfaced as a 500 on the cluster list, which is a page that then does
    not render at all. Reading the source means the next field upstream adds fails here,
    in a test named after the problem, instead of there.
    """
    import re
    from pathlib import Path

    lines = Path(source_file).read_text().split('\n')
    start = next(i for i, line in enumerate(lines) if line.startswith(f'def {function_name}'))
    end = next((i for i, line in enumerate(lines[start + 1:], start + 1)
                if line.startswith('@') or line.startswith('def ')), len(lines))
    body = '\n'.join(lines[start:end])

    guarded = set(re.findall(r"getattr\(mgr(?:\.config)?,\s*'([a-z_]+)'", body))
    manager_attrs = set(re.findall(r'\bmgr\.([a-z_]+)', body)) - {'config'} - guarded
    config_attrs = set(re.findall(r'\bmgr\.config\.([a-z_]+)', body)) - guarded
    return sorted(manager_attrs), sorted(config_attrs)


MANAGER_READS, CONFIG_READS = _unguarded_reads('get_clusters')


class TestTheQuestionsPegaproxAsks:
    """Every one of these is read somewhere without a getattr guard."""

    def test_it_declares_its_type(self, db):
        assert hyperv_cluster.HyperVClusterManager.cluster_type == 'hyperv'

    def test_the_enumeration_of_unguarded_reads_is_not_empty(self):
        # Guard against the source scan silently matching nothing — a rename upstream
        # would otherwise disable the two tests below without failing anything.
        assert len(MANAGER_READS) >= 3
        assert len(CONFIG_READS) >= 5

    @pytest.mark.parametrize('attribute', MANAGER_READS)
    def test_every_manager_attribute_the_cluster_list_reads_exists(self, db, attribute):
        assert hasattr(_cluster(db), attribute)

    @pytest.mark.parametrize('attribute', [
        'id', 'name', 'logger', 'ha_enabled', 'ha_node_status', 'nodes_in_maintenance',
    ])
    def test_the_attributes_other_pages_read_exist_too(self, db, attribute):
        # Not in get_clusters, but read without a guard elsewhere.
        assert hasattr(_cluster(db), attribute)

    @pytest.mark.parametrize('setting', CONFIG_READS)
    def test_every_config_field_the_cluster_list_serialises_exists(self, db, setting):
        # This is exactly what breaks with the ESXi facade: /api/clusters reads roughly
        # thirty config fields and raises on the first one that is absent.
        assert hasattr(_cluster(db).config, setting)

    @pytest.mark.parametrize('setting', [
        'user', 'ssh_user', 'ssh_key', 'ssh_port', 'api_port', 'ssl_verification',
        'excluded_nodes', 'ha_settings', 'api_token_user', 'api_token_secret',
    ])
    def test_the_config_fields_other_pages_read_exist_too(self, db, setting):
        assert hasattr(_cluster(db).config, setting)

    @pytest.mark.parametrize('method', [
        'connect', 'get_vms', 'get_vm_config', 'get_nodes', 'get_storages', 'get_networks',
        'get_node_status', 'get_vm_resources', 'get_vm_disks_for_export',
        'create_migration_snapshot', 'delete_migration_snapshot',
    ])
    def test_the_methods_the_migration_layer_calls_all_exist(self, db, method):
        assert callable(getattr(_cluster(db), method))

    def test_last_run_is_a_datetime_the_cluster_list_can_format(self, db):
        # The list calls .isoformat() on it when it is set, so a plain timestamp would
        # raise there rather than here.
        cluster = _cluster(db)
        assert cluster.last_run is not None
        assert cluster.last_run.isoformat()

    def test_a_host_that_never_connected_reports_no_last_run(self, db):
        manager = FakeManager(raises=HyperVError('unreachable', kind='unreachable'))
        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', CONFIG, manager=manager)
        cluster.connect()
        assert cluster.last_run is None

    def test_get_vm_resources_accepts_the_positional_max_age_callers_pass(self, db):
        # A known trap: callers pass max_age positionally, and a manager without the
        # parameter raises a TypeError far from here.
        from pegaprox.core import hyperv_inventory

        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        hyperv_inventory.read_now(cluster.id, cluster)
        assert len(cluster.get_vm_resources(0.0)) == 1
        assert cluster.get_vm_resources(0.0) == cluster.get_vm_resources()

    def test_the_generic_resource_question_never_reaches_the_host(self, db):
        # Asked of every manager once a second by the SSE broadcast loop. Answering it
        # from the host was one full WinRM inventory per second per source.
        manager = FakeManager(vms=[_summary(GUID_1)])
        cluster = _cluster(db, manager)
        before = manager.list_vms_calls
        assert cluster.get_vm_resources() == []
        assert cluster.datacenter_status()['guests']['vms']['total'] == 0
        assert manager.list_vms_calls == before

    def test_the_config_never_prints_the_password(self, db):
        config = _cluster(db).config
        assert config.pass_ not in repr(config)


class TestVocabulary:
    def test_a_host_presents_as_a_single_node(self, db):
        nodes = _cluster(db).get_nodes()
        assert len(nodes) == 1
        assert nodes[0]['node'] == 'Hyper-V site A'

    def test_vms_are_typed_as_full_machines(self, db):
        # The migration wizard filters on this; a Hyper-V VM is never a container.
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        assert cluster.get_vms()[0]['type'] == 'qemu'

    @pytest.mark.parametrize('hyperv_state, expected', [
        ('Off', 'stopped'), ('Saved', 'stopped'), ('Paused', 'stopped'),
        ('Running', 'running'), ('Starting', 'running'),
    ])
    def test_hyper_v_states_map_onto_the_two_words_the_lists_use(self, db, hyperv_state, expected):
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1, state=hyperv_state)]))
        assert cluster.get_vms()[0]['status'] == expected

    def test_the_real_hyper_v_state_is_carried_alongside(self, db):
        # 'stopped' loses the difference between Off and Saved, which matters for migration.
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1, state='Saved')]))
        assert cluster.get_vms()[0]['hyperv_state'] == 'Saved'

    def test_storage_is_empty_rather_than_invented(self, db):
        # Hyper-V disks are found through the VM that owns them. Inventing storage entries
        # would put unusable options in a target picker.
        assert _cluster(db).get_storages() == []

    def test_networks_are_the_switches_the_vms_actually_use(self, db):
        manager = FakeManager(
            vms=[_summary(GUID_1), _summary(GUID_2, 'second')],
            details={
                GUID_1: {'network_adapters': [{'switch_name': 'External'}]},
                GUID_2: {'network_adapters': [{'switch_name': 'Internal'},
                                              {'switch_name': 'External'}]},
            })
        assert [n['iface'] for n in _cluster(db, manager).get_networks()] == ['External', 'Internal']


class TestExportInventory:
    def _manager_with_detail(self):
        return FakeManager(
            vms=[_summary(GUID_1)],
            details={GUID_1: {
                'name': 'synthetic-vm', 'state': 'Off', 'cpu_count': 4, 'memory_mb': 4096,
                'generation': 2, 'checkpoint_count': 0, 'secure_boot_enabled': True,
                'vtpm_enabled': False, 'dynamic_memory_enabled': True,
                'network_adapters': [{'mac_address': 'AA'}],
                'disks': [{'path': 'C:\\VMs\\a.vhdx', 'controller_type': 'SCSI',
                           'controller_number': 0, 'controller_location': 0,
                           'size': 42 * 1024 ** 3, 'file_size': 8 * 1024 ** 3,
                           'vhd_type': 'Dynamic', 'parent_path': None, 'read_error': None,
                           'target_controller_hint': 'scsi'}]}})

    def test_the_planner_gets_the_shape_it_expects(self, db):
        cluster = _cluster(db, self._manager_with_detail())
        vmid = cluster.get_vms()[0]['vmid']
        result = cluster.get_vm_disks_for_export(vmid)
        assert 'data' in result
        assert result['data']['disks'][0]['capacity_gb'] == 42.0

    def test_the_hyper_v_specifics_the_mapping_needs_come_with_it(self, db):
        cluster = _cluster(db, self._manager_with_detail())
        data = cluster.get_vm_disks_for_export(cluster.get_vms()[0]['vmid'])['data']
        assert data['generation'] == 2
        assert data['secure_boot_enabled'] is True
        assert data['hyperv_guid'] == GUID_1

    def test_a_dynamic_disk_is_marked_thin(self, db):
        cluster = _cluster(db, self._manager_with_detail())
        data = cluster.get_vm_disks_for_export(cluster.get_vms()[0]['vmid'])['data']
        assert data['disks'][0]['thin'] is True

    def test_a_host_error_is_returned_with_its_remedy_not_raised(self, db):
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        result = cluster.get_vm_disks_for_export(cluster.get_vms()[0]['vmid'])
        assert 'error' in result
        assert 'remedy' in result


class TestExplicitlyAbsent:
    def test_no_migration_checkpoint_is_ever_taken(self, db):
        # It would be one more differencing file to merge before the disks could be read —
        # the opposite of what this migration needs.
        cluster = _cluster(db)
        assert 'error' in cluster.create_migration_snapshot(100)
        assert 'error' in cluster.delete_migration_snapshot(100)

    def test_the_refusal_explains_what_happens_instead(self, db):
        assert 'shut down' in _cluster(db).create_migration_snapshot(100)['error']


class TestWhatBootDoes:
    """The two hooks `app.py` calls, and the settings the shared cluster table cannot hold."""

    def test_registration_does_not_wait_for_an_unreachable_host(self, db):
        """A source that is switched off has to appear in the list as disconnected.

        Blocking here would instead add one connection timeout per unreachable host to
        every start-up, and the operator would not see the entry they need in order to
        fix it.
        """
        managers = {}
        manager = FakeManager(raises=HyperVError('No route to host', kind='unreachable'))
        original = hyperv_cluster.HyperVClusterManager._build_manager
        hyperv_cluster.HyperVClusterManager._build_manager = lambda self: manager
        try:
            hyperv_cluster.register_hyperv_source('hyperv-a', CONFIG, managers)
        finally:
            hyperv_cluster.HyperVClusterManager._build_manager = original

        assert 'hyperv-a' in managers
        assert managers['hyperv-a'].is_connected is False
        assert 'No route to host' in managers['hyperv-a'].connection_error

    def test_start_up_does_not_wait_for_a_host_that_is_slow_to_answer(self, db):
        """The registry is filled before any host is reached.

        `load_hyperv_sources` runs before the web server binds its port, so a connection
        attempt on this path costs the whole product its availability: two hosts that did
        not answer once kept PegaProx unreachable for half a minute after a restart. The
        gate below stands in for a host that accepts the connection and then says nothing.
        The sibling test above covers the same promise for the route that adds a host; this
        one covers start-up, which is where it was actually broken.
        """
        import threading

        from pegaprox.core import hyperv_db

        released = threading.Event()

        class SlowManager(FakeManager):
            def host_facts(self):
                # Bounded, so a regression fails the assertion below instead of hanging
                # the suite: synchronous code would return here connected.
                released.wait(timeout=10)
                return super().host_facts()

        hyperv_db.save_host(db.conn, db._encrypt, 'hyperv-slow',
                            {'name': 'slow', 'host': 'probe-host.example', 'user': 'svc',
                             'pass': 'fixture-' + 'not-a-real-credential'})
        managers = {}
        original = hyperv_cluster.HyperVClusterManager._build_manager
        hyperv_cluster.HyperVClusterManager._build_manager = lambda self: SlowManager()
        try:
            count = hyperv_cluster.load_hyperv_sources(managers)

            assert count == 1
            assert 'hyperv-slow' in managers, 'the route must find the source immediately'
            assert managers['hyperv-slow'].is_connected is False, \
                'start-up waited for the host instead of letting it connect in the background'
        finally:
            released.set()
            hyperv_cluster.HyperVClusterManager._build_manager = original

    def test_the_background_connection_still_records_what_the_host_said(self, db):
        """Not waiting must not mean not knowing: the state has to arrive on its own."""
        from pegaprox.core import hyperv_db

        hyperv_db.save_host(db.conn, db._encrypt, 'hyperv-dead',
                            {'name': 'dead', 'host': 'probe-host.example', 'user': 'svc',
                             'pass': 'fixture-' + 'not-a-real-credential'})
        managers = {}
        original = hyperv_cluster.HyperVClusterManager._build_manager
        failing = FakeManager(raises=HyperVError('No route to host', kind='unreachable'))
        hyperv_cluster.HyperVClusterManager._build_manager = lambda self: failing
        try:
            hyperv_cluster.load_hyperv_sources(managers)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if managers['hyperv-dead'].connection_error:
                    break
                time.sleep(0.01)
        finally:
            hyperv_cluster.HyperVClusterManager._build_manager = original

        assert managers['hyperv-dead'].is_connected is False
        assert 'No route to host' in managers['hyperv-dead'].connection_error

    def test_saving_the_cluster_configuration_leaves_a_migration_source_out_of_it(self, db):
        """A source shares the registry with the clusters; it must not share their table.

        Start-up builds a PegaProxManager for every entry in the cluster configuration, so
        a Hyper-V host written there comes back as a poll thread logging in to a Proxmox
        API it does not have -- the failure `migrate_hyperv_out_of_cluster_config` exists
        to clean up after. `save_config` runs on any cluster edit, so without the guard
        that cleanup is undone between two restarts.
        """
        from pegaprox.core import config as config_mod
        from pegaprox.globals import cluster_managers

        written = []
        original_save = db.save_cluster
        db.save_cluster = lambda cid, data: written.append(cid)

        source = hyperv_cluster.HyperVClusterManager('hyperv-a', CONFIG, manager=FakeManager())
        cluster_managers['hyperv-a'] = source
        try:
            config_mod.save_config()
        finally:
            cluster_managers.pop('hyperv-a', None)
            db.save_cluster = original_save

        assert 'hyperv-a' not in written, \
            'the migration source was written into the cluster configuration'

    def test_a_custom_winrm_port_survives_the_shared_cluster_table(self, db):
        """That table has no `port` column and rounds a port through `api_port`.

        Without reading both, a host configured on a non-default WinRM port would revert
        to 5986 on the first restart — and then fail to connect for a reason that looks
        like the host changed.
        """
        saved_shape = {**CONFIG, 'api_port': 15986}
        saved_shape.pop('port')

        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', saved_shape,
                                                      manager=FakeManager())
        assert cluster.config.port == 15986

    def test_a_host_without_a_port_defaults_to_the_http_listener(self, db):
        """The listener Windows creates by default is HTTP on 5985; an estate that never
        set up a certificate has nothing else to offer, and the default follows that."""
        config = {**CONFIG}
        config.pop('port')
        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', config, manager=FakeManager())
        assert cluster.config.use_ssl is False
        assert cluster.config.port == 5985
        assert cluster.config.auth == 'negotiate'
        assert cluster.config.encrypt_messages is True

    def test_choosing_https_without_a_port_defaults_to_5986(self, db):
        config = {**CONFIG, 'use_ssl': True}
        config.pop('port')
        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', config, manager=FakeManager())
        assert cluster.config.port == 5986

    def test_the_transport_settings_reach_the_connection(self, db, monkeypatch):
        """What the operator configured is what pypsrp gets, without this layer deciding
        that a transport is not good enough for it."""
        captured = {}

        def fake_client(connection):
            captured['connection'] = connection
            return FakeManager()

        monkeypatch.setattr(hyperv_cluster, 'PsrpHyperVClient', fake_client)
        monkeypatch.setattr(hyperv_cluster, 'HyperVManager', lambda *a, **kw: FakeManager())

        config = {**CONFIG, 'use_ssl': False, 'auth': 'basic', 'encrypt_messages': False,
                  'port': 5985}
        cluster = hyperv_cluster.HyperVClusterManager('hyperv-a', config)
        cluster._build_manager()

        connection = captured['connection']
        assert connection.use_ssl is False
        assert connection.port == 5985
        assert connection.auth == 'basic'
        assert connection.encrypt_messages is False

    def test_the_iso_library_survives_a_round_trip(self, db):
        """A source is stored in a table of its own, with a column for everything it has.

        Before this patch the host lived in the shared cluster row, which drops every key
        it has no column for -- and it has none for an ISO library or a share map.
        """
        from pegaprox.core import hyperv_db

        hyperv_db.save_host(db.conn, db._encrypt, 'hyperv-a', {
            'name': 'source-a', 'host': 'hv.invalid', 'user': 'svc', 'pass': 'secret',
            'port': 5986, 'ssl_verification': False,
            'iso_library_paths': ['C:\\iso', 'D:\\media'],
            'smb_share_map': {'D:': 'vhd$'}, 'smb_domain': 'EXAMPLE',
        })
        record = hyperv_db.load_host(db.conn, db._decrypt, 'hyperv-a')

        assert record['iso_library_paths'] == ['C:\\iso', 'D:\\media']
        assert record['ssl_verification'] is False
        assert record['smb_share_map'] == {'D:': 'vhd$'}
        assert record['smb_domain'] == 'EXAMPLE'

    def test_the_password_is_not_stored_in_clear(self, db):
        """Whoever reads the table directly must not find the credential in it."""
        from pegaprox.core import hyperv_db

        hyperv_db.save_host(db.conn, db._encrypt, 'hyperv-a', {
            'name': 'source-a', 'host': 'hv.invalid', 'user': 'svc', 'pass': 'secret'})
        stored = db.conn.execute(
            'SELECT pass_encrypted FROM hyperv_hosts WHERE id = ?',
            ('hyperv-a',)).fetchone()['pass_encrypted']

        assert stored != 'secret'
        assert 'secret' not in stored
        assert hyperv_db.load_host(db.conn, db._decrypt, 'hyperv-a')['pass'] == 'secret'

    def test_an_edit_without_a_new_password_keeps_the_stored_one(self, db):
        """A form that never echoes a password back sends an empty field on every edit."""
        from pegaprox.core import hyperv_db

        base = {'name': 'source-a', 'host': 'hv.invalid', 'user': 'svc', 'pass': 'secret'}
        hyperv_db.save_host(db.conn, db._encrypt, 'hyperv-a', base)
        hyperv_db.save_host(db.conn, db._encrypt, 'hyperv-a', {**base, 'pass': '',
                                                               'name': 'renamed'})
        record = hyperv_db.load_host(db.conn, db._decrypt, 'hyperv-a')

        assert record['name'] == 'renamed'
        assert record['pass'] == 'secret'

    def test_a_host_that_was_never_registered_reads_as_none(self, db):
        from pegaprox.core import hyperv_db
        assert hyperv_db.load_host(db.conn, db._decrypt, 'never-configured') is None

    def test_a_migration_left_running_by_a_restart_is_reconciled(self, db):
        """A row still saying 'running' describes a process that no longer exists.

        Without this the UI shows a transfer that will never move again, and nobody can
        tell whether it is stuck or dead.
        """
        from pegaprox.core import hyperv_db

        migration_id = hyperv_db.create_migration(
            db.conn, source_cluster='hyperv-a', source_vm_guid=GUID_1,
            source_vm_name='synthetic-vm', target_cluster='pve-1')

        interrupted = hyperv_cluster.sweep_interrupted_migrations()

        assert [row['migration_id'] for row in interrupted] == [migration_id]


class TestTheSharedNodeAndStoragePages:
    """The endpoints an operator reaches by clicking the host in the sidebar.

    Measured on a registered host: every one of these raised an AttributeError on the
    manager and reached the browser as a 500, so the Hyper-V views showed nothing at all —
    no VM, no status, no storage — and no migration could be started from the UI. The
    answers below are the honest empty ones; the point is that the page renders.
    """

    @pytest.mark.parametrize('method', [
        'get_node_summary', 'get_node_rrddata', 'get_node_network_config',
        'get_storage_list', '_get_node_ip', '_create_session',
    ])
    def test_the_method_exists(self, db, method):
        assert callable(getattr(_cluster(db), method))

    def test_the_node_summary_names_the_one_node_and_its_state(self, db):
        summary = _cluster(db).get_node_summary('Hyper-V site A')
        assert summary['node'] == 'Hyper-V site A'
        assert summary['status'] == 'online'

    @pytest.mark.parametrize('section', ['memory', 'swap', 'rootfs'])
    def test_the_summary_carries_every_figure_the_card_divides_by(self, db, section):
        # The card computes a percentage from used/total. A missing key renders NaN, which
        # is worse than the zero that renders as "no data".
        assert _cluster(db).get_node_summary('n')[section] == {'total': 0, 'used': 0, 'free': 0}

    def test_the_summary_says_why_the_figures_are_zero(self, db):
        assert 'migration source' in _cluster(db).get_node_summary('n')['note']

    def test_an_offline_host_says_so_rather_than_failing_to_render(self, db):
        cluster = hyperv_cluster.HyperVClusterManager(
            'hyperv-a', CONFIG, manager=FakeManager(raises=HyperVError('down', kind='unreachable')))
        cluster.connect()
        assert cluster.get_node_summary('n')['status'] == 'offline'

    def test_the_charts_get_empty_series_rather_than_an_error_banner(self, db):
        rrd = _cluster(db).get_node_rrddata('n', 'day')
        assert rrd['timeframe'] == 'day'
        assert rrd['timestamps'] == []
        assert set(rrd['metrics']) >= {'cpu', 'memory', 'net_in', 'net_out'}
        assert all(series == [] for series in rrd['metrics'].values())

    def test_the_timeframe_defaults_the_way_the_endpoint_expects(self, db):
        assert _cluster(db).get_node_rrddata('n')['timeframe'] == 'hour'

    def test_the_network_config_lists_the_switches_the_vms_are_on(self, db):
        manager = FakeManager(
            vms=[_summary(GUID_1)],
            details={GUID_1: {'network_adapters': [{'switch_name': 'External'},
                                                   {'switch_name': 'Internal'}]}})
        config = _cluster(db, manager).get_node_network_config('n')
        assert [row['iface'] for row in config] == ['External', 'Internal']
        assert {row['type'] for row in config} == {'vswitch'}

    def test_the_storage_list_is_empty_under_both_names(self, db):
        cluster = _cluster(db)
        assert cluster.get_storage_list('n') == cluster.get_storages('n') == []

    def test_the_one_node_resolves_to_the_host_it_was_registered_with(self, db):
        assert _cluster(db)._get_node_ip('Hyper-V site A') == CONFIG['host']

    def test_asking_for_a_proxmox_rest_session_is_refused_in_words(self, db):
        # Roughly two hundred call sites build /api2/json/... on whatever this returns.
        # Handing them a session would turn "does not apply" into a page that loads and
        # then shows nothing, with nothing in the log saying why.
        with pytest.raises(HyperVError) as caught:
            _cluster(db)._create_session()
        assert 'REST' in caught.value.message


def _detail(**extra):
    """A VM as the host describes it, in the shape normalise_vm_detail produces."""
    detail = dict(_summary(GUID_1, name='win2016'), **{
        'disks': [{'path': 'C:\\VMs\\win2016.vhdx', 'controller_type': 'SCSI',
                   'controller_number': 0, 'controller_location': 0,
                   'vhd_format': 'VHDX', 'vhd_type': 'Dynamic', 'parent_path': None,
                   'file_size': 9000000000, 'size': 137438953472, 'attached': True,
                   'read_error': None, 'target_controller_hint': 'scsi'}],
        'network_adapters': [{'name': 'Network Adapter', 'mac_address': '00155D010203',
                              'mac_address_colons': '00:15:5d:01:02:03',
                              'dynamic_mac': True, 'switch_name': 'External',
                              'connected': True, 'vlan_mode': 'Untagged', 'vlan_id': 0}],
        'checkpoints': [], 'checkpoint_count': 0,
        'secure_boot_enabled': True, 'secure_boot_template': 'MicrosoftWindows',
        'vtpm_enabled': False, 'boot_order': ['Drive'], 'dvd_drives': [],
        'memory_minimum_bytes': 536870912, 'memory_maximum_bytes': 8589934592,
        'dynamic_memory_enabled': False, 'configuration_version': '9.0',
        'bitlocker_state': None, 'virtio_driver_state': None,
        'total_disk_bytes': 137438953472,
    })
    detail.update(extra)
    return detail


def _config(db, **extra):
    cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)],
                                       details={GUID_1: _detail(**extra)}))
    result = cluster.get_vm_config(cluster.config.name, cluster.get_vms()[0]['vmid'], 'qemu')
    assert result['success'], result
    return result['config']


class TestTheVmDialogsHardwareTab:
    """The dialog an operator opens to see a VM's hardware.

    It asks every manager the same way — get_vm_config(node, vmid, vm_type), then `success`
    and `config`. Measured on a registered host: the Hyper-V adapter took one argument and
    returned the detail bare, so the call raised a TypeError and the dialog told the
    operator to check their connection. The connection was fine.
    """

    def test_the_shared_three_argument_call_is_answered(self, db):
        assert _config(db)['general']['name'] == 'win2016'

    def test_the_planners_two_argument_call_is_answered_too(self, db):
        # xhm.py calls get_vm_config(None, vmid) with no type.
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)],
                                           details={GUID_1: _detail()}))
        vmid = cluster.get_vms()[0]['vmid']
        assert cluster.get_vm_config(None, vmid)['success']

    def test_a_single_argument_is_read_as_the_vmid_not_the_node(self, db):
        # A node name is a string and a synthetic VMID is not, so this is decided here
        # rather than failing somewhere that cannot tell the two apart any more.
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)],
                                           details={GUID_1: _detail()}))
        vmid = cluster.get_vms()[0]['vmid']
        assert cluster.get_vm_config(vmid)['success']

    def test_an_unknown_vmid_is_a_failed_envelope_not_a_raise(self, db):
        result = _cluster(db).get_vm_config('some-node', 999999, 'qemu')
        assert result['success'] is False
        assert 'error' in result

    def test_the_processor_and_memory_are_the_hosts_numbers(self, db):
        hardware = _config(db)['hardware']
        assert hardware['cores'] == 4
        assert hardware['memory'] == 4096

    @pytest.mark.parametrize('generation, bios, machine', [
        (2, 'ovmf', 'q35'), (1, 'seabios', '')])
    def test_the_generation_is_shown_as_the_firmware_it_means(self, db, generation, bios,
                                                              machine):
        hardware = _config(db, generation=generation)['hardware']
        assert (hardware['bios'], hardware['machine']) == (bios, machine)

    def test_an_unknown_generation_leaves_the_firmware_empty_rather_than_guessing(self, db):
        hardware = _config(db, generation=None)['hardware']
        assert (hardware['bios'], hardware['machine']) == ('', '')

    def test_a_disk_keeps_the_controller_position_it_sits_on(self, db):
        assert _config(db)['disks'][0]['id'] == 'scsi0'

    def test_a_disk_without_a_reported_position_still_gets_its_own_row(self, db):
        # Two disks collapsing onto one id is how one of them stops being shown at all.
        detail = _detail()
        detail['disks'] = [dict(detail['disks'][0], controller_location=None),
                           dict(detail['disks'][0], controller_location=None,
                                path='D:\\VMs\\data.vhdx')]
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)], details={GUID_1: detail}))
        disks = cluster.get_vm_config('n', cluster.get_vms()[0]['vmid'], 'qemu')['config']['disks']
        assert len({disk['id'] for disk in disks}) == 2

    def test_the_disk_carries_its_format_and_provisioning(self, db):
        disk = _config(db)['disks'][0]
        assert (disk['format'], disk['provisioning']) == ('VHDX', 'Dynamic')

    def test_the_adapter_is_shown_on_the_switch_it_is_attached_to(self, db):
        network = _config(db)['networks'][0]
        assert network['id'] == 'net0'
        assert network['bridge'] == 'External'
        assert network['macaddr'] == '00:15:5d:01:02:03'

    @pytest.mark.parametrize('key, expected', [
        ('generation', 2), ('secure_boot_enabled', True), ('vtpm_enabled', False),
        ('checkpoint_count', 0), ('configuration_version', '9.0'),
        ('memory_maximum_bytes', 8589934592),
    ])
    def test_what_a_migration_turns_on_is_carried_where_proxmox_has_no_key(self, db, key,
                                                                          expected):
        # Secure Boot, a vTPM and a checkpoint decide whether an import succeeds, and none
        # of them has a Proxmox config key to be squeezed into.
        assert _config(db)['hyperv'][key] == expected

    def test_the_hosts_own_answer_is_carried_through_unreduced(self, db):
        assert _config(db)['raw']['guid'] == GUID_1

    def test_the_dialog_is_told_the_source_may_not_be_edited(self, db):
        config = _config(db)
        assert config['read_only'] is True
        assert 'only reads' in config['read_only_reason']

    @pytest.mark.parametrize('state, expected', [('Running', 'running'), ('Off', 'stopped')])
    def test_the_running_state_reaches_the_dialog(self, db, state, expected):
        assert _config(db, state=state)['status']['status'] == expected

    def test_nothing_proxmox_only_is_filled_with_a_default_that_invites_an_edit(self, db):
        hardware = _config(db)['hardware']
        assert hardware['cpu'] == ''
        assert hardware['scsihw'] == ''


class TestTheLockColumnOnAVmRow:
    """Measured in the instance log: every VM row asked, and every row logged an error.

    `'HyperVClusterManager' object has no attribute 'get_vm_lock_status'` — once per VM,
    for a question whose answer is always no.
    """

    def test_the_method_exists(self, db):
        assert callable(_cluster(db).get_vm_lock_status)

    def test_nothing_on_a_hyper_v_source_is_locked(self, db):
        status = _cluster(db).get_vm_lock_status('a-node', 100, 'qemu')
        assert status['success'] is True
        assert status['locked'] is False


class TestWhetherTheSourceRestartsByItself:
    """Hyper-V's AutomaticStartAction, in the vocabulary the shared VM dialog speaks.

    Proxmox calls the same idea onboot. Mapping onto it means the dialog shows the fact
    without being taught anything about Hyper-V - which is the whole point of translating
    into that envelope rather than adding a panel beside it.
    """

    def _config(self, **detail):
        base = {'name': 'synthetic', 'cpu_count': 2, 'memory_mb': 2048,
                'generation': 2, 'disks': [], 'network_adapters': []}
        base.update(detail)
        return hyperv_cluster._proxmox_shaped_config(base, 'host', 'qemu')

    def test_a_source_that_stays_down_reads_as_onboot_off(self):
        assert self._config(automatic_start_action='Nothing')['options']['onboot'] == 0

    def test_a_source_that_comes_back_reads_as_onboot_on(self):
        assert self._config(automatic_start_action='StartIfRunning')['options']['onboot'] == 1
        assert self._config(automatic_start_action='Start')['options']['onboot'] == 1

    def test_an_unknown_setting_is_not_reported_as_harmless(self):
        """Absent must not read as 'will not restart'; that is the reassuring answer."""
        assert self._config()['options']['onboot'] == 0

    def test_the_exact_setting_survives_the_translation(self):
        """onboot is a yes/no. Start and StartIfRunning differ in when they fire."""
        config = self._config(automatic_start_action='StartIfRunning',
                              automatic_start_delay_seconds=45,
                              automatic_stop_action='Save')
        assert config['hyperv']['automatic_start_action'] == 'StartIfRunning'
        assert config['hyperv']['automatic_start_delay_seconds'] == 45
        assert config['hyperv']['automatic_stop_action'] == 'Save'


class TestAHostAnEarlierBuildLeftInTheClusterConfig:
    """An upgraded instance carries the same host twice, and the duplicate is not idle.

    Start-up builds a manager for every entry in the cluster configuration. Anything that
    is not XCP-ng used to become a PegaProxManager, whose poll thread logs in to a Proxmox
    API a Hyper-V host does not have and retries at the zero interval such an entry
    carries. Measured on an upgraded instance: about a hundred and twenty log lines a
    second, three gigabytes in twelve hours, and a filesystem full enough to fail an
    unrelated migration.
    """

    @staticmethod
    def _config(monkeypatch, entries):
        monkeypatch.setattr('pegaprox.core.config.load_config', lambda: entries)

    def test_the_stale_entry_is_removed(self, monkeypatch, db_conn):
        from pegaprox.core import hyperv_cluster

        deleted = []
        self._config(monkeypatch, {
            'h1': {'name': 'a-host', 'cluster_type': 'hyperv', 'host': '127.0.0.1'},
            'p1': {'name': 'a-cluster', 'cluster_type': 'proxmox', 'host': '127.0.0.1'},
        })
        monkeypatch.setattr('pegaprox.core.db.get_db',
                            lambda: _FakeDb(db_conn, deleted))

        moved = hyperv_cluster.migrate_hyperv_out_of_cluster_config()

        assert moved == 1
        assert deleted == ['h1'], 'only the Hyper-V entry may be removed'

    def test_a_host_not_yet_in_its_own_table_is_carried_over_first(self, monkeypatch,
                                                                  db_conn):
        """Removing it before saving it would lose the registration."""
        from pegaprox.core import hyperv_cluster, hyperv_db

        deleted = []
        self._config(monkeypatch, {
            'h1': {'name': 'a-host', 'cluster_type': 'hyperv', 'host': '127.0.0.1',
                   'user': 'Administrator', 'pass': 'x', 'port': 5986},
        })
        monkeypatch.setattr('pegaprox.core.db.get_db',
                            lambda: _FakeDb(db_conn, deleted))

        hyperv_cluster.migrate_hyperv_out_of_cluster_config()

        carried = hyperv_db.load_hosts(db_conn, lambda v: v)
        assert [record['id'] for record in carried] == ['h1']

    def test_nothing_happens_without_a_stale_entry(self, monkeypatch, db_conn):
        from pegaprox.core import hyperv_cluster

        deleted = []
        self._config(monkeypatch, {'p1': {'name': 'a', 'cluster_type': 'proxmox'}})
        monkeypatch.setattr('pegaprox.core.db.get_db',
                            lambda: _FakeDb(db_conn, deleted))

        assert hyperv_cluster.migrate_hyperv_out_of_cluster_config() == 0
        assert deleted == []


class _FakeDb:
    """Enough of the database for the migration: the host table and a delete recorder."""

    def __init__(self, conn, deleted):
        self.conn = conn
        self._deleted = deleted

    @staticmethod
    def _encrypt(value):
        return value

    @staticmethod
    def _decrypt(value):
        return value

    def delete_cluster(self, cluster_id):
        self._deleted.append(cluster_id)


@pytest.fixture
def db_conn(db):
    """A connection whose schema includes the Hyper-V tables."""
    return db.conn
