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

import pytest

from pegaprox.core import hyperv_cluster
from pegaprox.core.hyperv import HyperVManager
from pegaprox.core.hyperv_errors import HyperVError, KIND_AUTHORIZATION

GUID_1 = '11111111-1111-1111-1111-111111111111'
GUID_2 = '22222222-2222-2222-2222-222222222222'

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

    def host_facts(self):
        if self._raises:
            raise self._raises
        return self._facts

    def verify_properties(self):
        return self._properties

    def list_vms(self):
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
        assert 'error' in cluster.get_vm_config(999999)
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
        cluster = _cluster(db, FakeManager(vms=[_summary(GUID_1)]))
        assert cluster.get_vm_resources(0.0) == cluster.get_vm_resources()

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

    def test_the_iso_library_is_stored_where_it_is_not_dropped(self, db):
        """The shared cluster table drops every key it has no column for."""
        from pegaprox.core import hyperv_db

        hyperv_db.save_host_settings(db.conn, 'hyperv-a', winrm_port=5986,
                                     verify_certificate=False,
                                     iso_library_paths=['C:\\iso', 'D:\\media'])
        settings = hyperv_db.load_host_settings(db.conn, 'hyperv-a')

        assert settings['iso_library_paths'] == ['C:\\iso', 'D:\\media']
        assert settings['ssl_verification'] is False

    def test_a_host_with_nothing_saved_reads_as_empty_not_as_missing(self, db):
        from pegaprox.core import hyperv_db
        assert hyperv_db.load_host_settings(db.conn, 'never-configured') == {}

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
