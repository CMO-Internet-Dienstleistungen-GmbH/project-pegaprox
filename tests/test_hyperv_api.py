# The Hyper-V routes, driven end to end through the real Flask app.
#
# These assert the two things a unit test of the handler body cannot: that the routes are
# actually reachable at the paths the UI will call, and that an unauthorized caller is
# stopped by the stack rather than by a check somebody remembered to write. The manager is
# faked; everything above it — require_auth, the CSRF gate, check_cluster_access and the
# per-VM gate — is the real code.
#
# The authorization cases are deliberately the ones that have historically gone wrong in
# this product: a caller who reaches a cluster through a single VM-ACL entry and then asks
# for a different VM, and a route that gates the cluster but forgets the object.

import pytest

from pegaprox.core.hyperv_errors import (
    HyperVError, KIND_AUTHORIZATION, KIND_CLIENT_DEPENDENCY, KIND_TIMEOUT, KIND_UNREACHABLE,
)


HOST = 'hv_1'
VM_A = 100
VM_B = 101
GUID_A = '11111111-1111-1111-1111-111111111111'
GUID_B = '22222222-2222-2222-2222-222222222222'


def _vm_row(vmid, guid, name):
    return {'vmid': vmid, 'name': name, 'status': 'stopped', 'type': 'qemu',
            'node': HOST, 'maxmem': 4294967296, 'maxcpu': 2,
            'hyperv_state': 'Off', 'hyperv_guid': guid, 'generation': 2}


def _vm_detail(vmid=VM_A):
    return {
        'vmid': vmid, 'guid': GUID_A, 'name': 'guest-a', 'state': 'Off', 'generation': 2,
        'cpu_count': 2, 'memory_mb': 4096, 'memory_startup_bytes': 4294967296,
        'dynamic_memory_enabled': False, 'checkpoint_count': 0, 'checkpoints': [],
        'secure_boot_enabled': False, 'vtpm_enabled': False,
        'bitlocker_state': None, 'virtio_driver_state': None,
        'disks': [{'path': 'C:\\vm\\a.vhdx', 'size': 42949672960, 'file_size': 8589934592,
                   'vhd_type': 'Dynamic', 'parent_path': None, 'controller_type': 'SCSI',
                   'controller_number': 0, 'controller_location': 0,
                   'target_controller_hint': 'scsi', 'read_error': None}],
        'network_adapters': [{'name': 'Network Adapter', 'mac_address': '00:15:5d:00:00:01',
                              'switch_name': 'External'}],
        'total_disk_bytes': 42949672960,
    }


def _hyperv_manager(api, *, vms=None, detail=None, iso_paths=('C:\\iso',)):
    """A fake Hyper-V host registered under HOST, with the reads a route performs stubbed."""
    fake = api.make_fake_manager(cluster_id=HOST, cluster_type='hyperv')
    fake.id = HOST
    fake.name = HOST
    fake.is_connected = True
    fake.connection_error = ''
    fake.property_report = {'complete': True, 'missing': []}
    fake.config.iso_library_paths = list(iso_paths)
    fake.get_vms.return_value = vms if vms is not None else [
        _vm_row(VM_A, GUID_A, 'guest-a'), _vm_row(VM_B, GUID_B, 'guest-b')]
    fake.vm_detail.return_value = detail if detail is not None else _vm_detail()
    fake.guid_for.side_effect = lambda vmid: {VM_A: GUID_A, VM_B: GUID_B}.get(int(vmid))
    api.set_manager(HOST, fake)
    return fake


# ===========================================================================
# The routes exist where the UI will look for them
# ===========================================================================

@pytest.mark.parametrize('rule', [
    '/api/hyperv/<cluster_id>/host',
    '/api/hyperv/<cluster_id>/isos',
    '/api/hyperv/<cluster_id>/vms',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>/disks',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>/state',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>/preflight',
    '/api/hyperv/<cluster_id>/migrations/<migration_id>/post-import',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>/start',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>/shutdown',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>/checkpoints',
    '/api/hyperv/<cluster_id>/vms/<int:vmid>/iso',
    '/api/hyperv/hosts',
    '/api/hyperv/hosts/<host_id>',
    '/api/hyperv/<cluster_id>/migrations',
    '/api/hyperv/<cluster_id>/migrations/<migration_id>/cleanup',
])
def test_the_route_is_registered(api, rule):
    """A blueprint that is written but never registered fails silently at runtime."""
    assert rule in {str(r.rule) for r in api.app.url_map.iter_rules()}


def test_no_hyperv_route_can_change_the_source_beyond_preparing_it(api):
    """The source is a customer's hypervisor, not one this product manages.

    The whole allowed vocabulary is start, shut down, remove checkpoints and swap an ISO.
    A route that created, deleted or reconfigured a VM would be a capability nobody asked
    for on a machine nobody handed over.
    """
    hyperv_rules = [r for r in api.app.url_map.iter_rules()
                    if str(r.rule).startswith('/api/hyperv/')]
    writing = {(str(r.rule), m) for r in hyperv_rules
               for m in r.methods if m in ('POST', 'PUT', 'PATCH', 'DELETE')}
    assert writing == {
        ('/api/hyperv/<cluster_id>/vms/<int:vmid>/start', 'POST'),
        ('/api/hyperv/<cluster_id>/vms/<int:vmid>/shutdown', 'POST'),
        ('/api/hyperv/<cluster_id>/vms/<int:vmid>/checkpoints', 'DELETE'),
        ('/api/hyperv/<cluster_id>/vms/<int:vmid>/iso', 'POST'),
        ('/api/hyperv/<cluster_id>/vms/<int:vmid>/iso', 'DELETE'),
        # A preflight is a read that needs a body. It changes nothing on either side.
        ('/api/hyperv/<cluster_id>/vms/<int:vmid>/preflight', 'POST'),
        # The one destructive route, and it points the other way: it removes what a failed
        # migration created on the *target*. The source is never part of a cleanup, which
        # is what makes "start the original again" a rollback that still works.
        ('/api/hyperv/<cluster_id>/migrations/<migration_id>/cleanup', 'POST'),
        # The post-import routes point the same way. They change the VM this product
        # created on Proxmox -- its CD drive, its recorded driver state, its disk
        # controller and network model -- and reach the Hyper-V host for nothing but the
        # permission check. The source keeps running on its original hardware, which is
        # the rollback for everything these three do.
        ('/api/hyperv/<cluster_id>/migrations/<migration_id>/virtio-iso', 'POST'),
        ('/api/hyperv/<cluster_id>/migrations/<migration_id>/drivers', 'POST'),
        ('/api/hyperv/<cluster_id>/migrations/<migration_id>/profile', 'POST'),
        # Registering, editing and removing a source. These write PegaProx's own record of
        # which hosts exist -- a Hyper-V host is a migration source, not a cluster, so it is
        # managed here rather than through /api/clusters (docs/adr/0001). None of them
        # reaches the hypervisor for anything but the connection test.
        ('/api/hyperv/hosts', 'POST'),
        ('/api/hyperv/hosts/<host_id>', 'PUT'),
        ('/api/hyperv/hosts/<host_id>', 'DELETE'),
    }


# ===========================================================================
# Authorization
# ===========================================================================

def test_an_anonymous_caller_reaches_nothing(api):
    _hyperv_manager(api)
    assert api.anon().get(f'/api/hyperv/{HOST}/vms').status_code == 401


def test_a_user_outside_the_tenant_cannot_read_a_vm(api, seed):
    """The cluster gate, in its own right: no tenant grant, no ACL, no pool."""
    _hyperv_manager(api)
    seed.tenant('other', clusters=['some_other_cluster'])
    user = seed.user('outsider', role='user', tenant_id='other')

    response = api.as_user(user).get(f'/api/hyperv/{HOST}/vms/{VM_A}')
    assert response.status_code == 403


def test_a_vm_acl_grant_does_not_open_the_neighbouring_vm(api, seed):
    """The case a cluster-only gate gets wrong.

    An ACL entry on one VM is what lets this caller reach the host at all. Without the
    per-VM gate the same request would then hand back any other VM on it.
    """
    _hyperv_manager(api)
    seed.tenant('other', clusters=[])
    user = seed.user('scoped', role='user', tenant_id='other')
    seed.vm_acl(HOST, VM_A, ['scoped'])

    client = api.as_user(user)
    assert client.get(f'/api/hyperv/{HOST}/vms/{VM_A}').status_code == 200
    assert client.get(f'/api/hyperv/{HOST}/vms/{VM_B}').status_code == 403


def test_the_vm_list_shows_only_the_vms_the_caller_may_see(api, seed):
    """Reaching a host through one VM must not hand back its inventory."""
    _hyperv_manager(api)
    seed.tenant('other', clusters=[])
    user = seed.user('scoped', role='user', tenant_id='other')
    seed.vm_acl(HOST, VM_A, ['scoped'])

    body = api.as_user(user).get(f'/api/hyperv/{HOST}/vms').get_json()
    assert [vm['vmid'] for vm in body['vms']] == [VM_A]


def test_an_admin_sees_every_vm(api, seed):
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')
    body = api.as_user(admin).get(f'/api/hyperv/{HOST}/vms').get_json()
    assert [vm['vmid'] for vm in body['vms']] == [VM_A, VM_B]


@pytest.mark.parametrize('method,path', [
    ('post', f'/api/hyperv/{HOST}/vms/{VM_A}/start'),
    ('post', f'/api/hyperv/{HOST}/vms/{VM_A}/shutdown'),
    ('delete', f'/api/hyperv/{HOST}/vms/{VM_A}/checkpoints'),
    ('post', f'/api/hyperv/{HOST}/vms/{VM_A}/iso'),
])
def test_a_viewer_can_look_but_not_act(api, seed, method, path):
    """A viewer holds hyperv.vm.view and nothing that changes the source."""
    _hyperv_manager(api)
    viewer = seed.user('readonly', role='viewer')
    client = api.as_user(viewer)

    assert client.get(f'/api/hyperv/{HOST}/vms/{VM_A}').status_code == 200
    assert getattr(client, method)(path, json={'path': 'C:\\iso\\virtio.iso'}).status_code == 403


# ===========================================================================
# Addressing
# ===========================================================================

def test_a_proxmox_cluster_id_is_refused_rather_than_crashed_into(api, seed):
    """Without the type check these routes would call Hyper-V methods on a Proxmox
    manager and fail with an AttributeError instead of saying what is wrong."""
    api.set_manager('pve_1', api.make_fake_manager(cluster_id='pve_1', cluster_type='proxmox'))
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).get('/api/hyperv/pve_1/vms')
    assert response.status_code == 400
    assert 'not a Hyper-V host' in response.get_json()['error']


def test_an_unknown_cluster_is_a_404(api, seed):
    admin = seed.user('root', role='admin')
    assert api.as_user(admin).get('/api/hyperv/nope/vms').status_code == 404


def test_a_vmid_this_host_never_issued_is_a_404(api, seed):
    """The GUID is resolved from the durable map, never taken from the request.

    That is what keeps a VMID-based ACL meaningful: a caller cannot address a VM the ACL
    was never written for by naming its Hyper-V GUID directly.
    """
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).get(f'/api/hyperv/{HOST}/vms/999/disks')
    assert response.status_code == 404


# ===========================================================================
# How a failure on the host reaches the caller
# ===========================================================================

@pytest.mark.parametrize('kind,status', [
    (KIND_UNREACHABLE, 502),
    (KIND_AUTHORIZATION, 502),
    (KIND_TIMEOUT, 504),
    (KIND_CLIENT_DEPENDENCY, 503),
])
def test_a_source_failure_keeps_its_classification(api, seed, kind, status):
    """Four causes, four answers.

    They share a status family on purpose — none of them is the caller's own
    authorization, and a 401 or 403 here would tell a browser its session had expired.
    What separates them is `kind` and the remedy that comes with it, which is what an
    operator acts on.
    """
    fake = _hyperv_manager(api)
    fake.get_vms.side_effect = HyperVError('the host said no', kind=kind)
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).get(f'/api/hyperv/{HOST}/vms')
    assert response.status_code == status
    body = response.get_json()
    assert body['kind'] == kind
    assert body['remedy']


def test_a_disconnected_host_still_answers_its_own_status(api, seed):
    """An operator has to be able to see why a source is down, from the source's page."""
    fake = _hyperv_manager(api)
    fake.is_connected = False
    fake.connect.return_value = False
    fake.connection_error = 'certificate verify failed'
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).get(f'/api/hyperv/{HOST}/host')
    assert response.status_code == 200
    body = response.get_json()
    assert body['connected'] is False
    assert body['connection_error'] == 'certificate verify failed'


def test_the_host_page_reports_properties_the_host_does_not_expose(api, seed):
    """Hyper-V documents its cmdlets' parameters, almost never the objects they return.

    A property that is absent reads as null rather than raising, which would make a VM
    look like it had no generation and no checkpoints. Naming the gap is the difference
    between a visible warning and a silently wrong inventory.
    """
    fake = _hyperv_manager(api)
    fake.property_report = {'complete': False, 'missing': ['Generation']}
    fake.manager.host_facts.return_value = {'powershell_version': '5.1.20348.2849'}
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).get(f'/api/hyperv/{HOST}/host').get_json()
    assert body['properties']['missing'] == ['Generation']


# ===========================================================================
# Actions
# ===========================================================================

def test_shutdown_asks_the_guest_and_never_cuts_the_power(api, seed):
    """Only the orderly shutdown exists.

    Removing the power leaves the disks in the state an unexpected outage leaves them in,
    which is the one state a migration must not start from.
    """
    fake = _hyperv_manager(api)
    fake.manager.shutdown_vm.return_value = {'state': 'Off'}
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/shutdown', json={})
    assert response.status_code == 200
    fake.manager.shutdown_vm.assert_called_once()
    assert fake.manager.shutdown_vm.call_args.args[0] == GUID_A
    # There is no turn-off path on the manager for this route to have reached for.
    assert not hasattr(type(fake.manager), 'turn_off_vm')


def test_a_nonsense_shutdown_timeout_is_refused_before_the_host_is_touched(api, seed):
    fake = _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/shutdown',
                                       json={'timeout_seconds': 0})
    assert response.status_code == 400
    fake.manager.shutdown_vm.assert_not_called()


def test_removing_checkpoints_is_audited(api, seed):
    """It destroys states on a customer's source that Hyper-V cannot bring back."""
    from pegaprox.utils import audit

    fake = _hyperv_manager(api)
    fake.manager.remove_checkpoints.return_value = {'removed': 2}
    admin = seed.user('root', role='admin')

    written = []
    original = audit.log_audit
    try:
        import pegaprox.api.hyperv as hyperv_api
        hyperv_api.log_audit = lambda *a, **k: written.append(a)
        response = api.as_user(admin).delete(f'/api/hyperv/{HOST}/vms/{VM_A}/checkpoints',
                                             json={})
    finally:
        import pegaprox.api.hyperv as hyperv_api
        hyperv_api.log_audit = original

    assert response.status_code == 200
    assert any('checkpoint' in entry[1] for entry in written)


def test_an_iso_outside_the_configured_library_is_refused(api, seed):
    """Taking an arbitrary path from the request would be a filesystem read primitive on
    the customer's host, not a media action."""
    fake = _hyperv_manager(api)
    fake.manager.list_isos.return_value = [{'path': 'C:\\iso\\virtio.iso', 'name': 'virtio.iso'}]
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/iso',
                                       json={'path': 'C:\\Windows\\System32\\config\\SAM'})
    assert response.status_code == 400
    fake.manager.mount_iso.assert_not_called()


def test_an_iso_from_the_library_is_mounted(api, seed):
    fake = _hyperv_manager(api)
    fake.manager.list_isos.return_value = [{'path': 'C:\\iso\\virtio.iso', 'name': 'virtio.iso'}]
    fake.manager.mount_iso.return_value = {'mounted': True}
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/iso',
                                       json={'path': 'C:\\iso\\virtio.iso'})
    assert response.status_code == 200
    fake.manager.mount_iso.assert_called_once_with(GUID_A, 'C:\\iso\\virtio.iso')


def test_a_host_with_no_iso_library_says_so_instead_of_browsing_for_one(api, seed):
    """PegaProx does not walk a customer's filesystem looking for ISOs."""
    _hyperv_manager(api, iso_paths=())
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).get(f'/api/hyperv/{HOST}/isos').get_json()
    assert body['isos'] == []
    assert 'No ISO library path' in body['message']


# ===========================================================================
# Preflight
# ===========================================================================

def test_preflight_runs_every_check_rather_than_stopping_at_the_first(api, seed):
    """Somebody preparing a VM wants the whole list, not one item per attempt."""
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/preflight',
                                   json={}).get_json()
    checks = {f['check'] for f in body['findings']}
    assert {'power_state', 'checkpoints', 'firmware', 'disk_type', 'target_capacity',
            'network_mapping', 'secure_boot', 'vtpm', 'bitlocker', 'virtio_drivers',
            'source_access'} <= checks


def test_preflight_only_offers_the_one_direction_this_patch_supports(api, seed):
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/preflight',
                                   json={}).get_json()
    assert body['direction'] == 'hyperv_to_pve'


def test_preflight_refuses_a_target_that_is_not_proxmox(api, seed):
    _hyperv_manager(api)
    api.set_manager('xen_1', api.make_fake_manager(cluster_id='xen_1', cluster_type='xcpng'))
    admin = seed.user('root', role='admin')

    response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/preflight',
                                       json={'target_cluster': 'xen_1'})
    assert response.status_code == 400
    assert 'only migrate to Proxmox' in response.get_json()['error']


def test_an_unknown_target_capacity_blocks(api, seed):
    """Starting a copy without knowing whether it fits risks filling a storage that other
    guests already run on — damage to VMs nobody was migrating."""
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/preflight',
                                   json={}).get_json()
    capacity = next(f for f in body['findings'] if f['check'] == 'target_capacity')
    assert capacity['severity'] == 'blocking'
    assert body['blocked'] is True


def test_the_unbuilt_transport_is_an_unknown_to_confirm_not_a_silent_pass(api, seed):
    """The file-share transport is not wired into this route yet.

    That has to read as an explicit unknown somebody confirms. An OK would be a claim
    nothing checked, and a blocker would stop a VM that is otherwise ready.
    """
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/preflight',
                                   json={}).get_json()
    access = next(f for f in body['findings'] if f['check'] == 'source_access')
    assert access['severity'] == 'warning'
    assert 'source_access' in body['requires_acknowledgement']


def test_the_driver_check_describes_the_hardware_the_import_actually_creates(api, seed):
    """The route must not carry a controller default of its own.

    It did, and it disagreed with the one the planner and the runner use: this route
    assumed VirtIO SCSI while `target_hardware` attaches the disks to SATA. The result
    was a warning about missing VirtIO drivers on a migration that never needs them —
    an acknowledgeable warning, so somebody had to confirm a risk that did not exist.
    """
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/preflight',
                                   json={}).get_json()
    drivers = next(f for f in body['findings'] if f['check'] == 'virtio_drivers')
    assert drivers['severity'] == 'ok'
    assert 'sata' in (drivers['summary'] + drivers.get('detail', '')).lower()
    assert 'virtio_drivers' not in body['requires_acknowledgement']


def test_naming_the_virtio_hardware_says_the_drivers_will_be_installed(api, seed):
    """The check follows the choice, and asking for VirtIO now means asking for drivers.

    It used to be a risk to confirm, because a guest reaching VirtIO hardware without its
    driver does not boot. The migration writes the driver into the disk before anything
    starts the VM, so the confirmation would be asking the operator to accept a risk the
    same screen has just offered to remove. What it must not do is fall silent: the
    finding stays in the list and says what will happen.
    """
    _hyperv_manager(api)
    admin = seed.user('root', role='admin')

    body = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/preflight',
                                   json={'hardware': 'virtio'}).get_json()
    drivers = next(f for f in body['findings'] if f['check'] == 'virtio_drivers')
    assert drivers['severity'] == 'ok'
    assert 'writes the VirtIO drivers' in drivers['summary']
    assert 'virtio_drivers' not in body['requires_acknowledgement']


def test_preflight_cannot_be_reached_for_a_vm_the_caller_may_not_see(api, seed):
    """The verdict names disks, paths and adapters — it is a read like any other."""
    _hyperv_manager(api)
    seed.tenant('other', clusters=[])
    user = seed.user('scoped', role='user', tenant_id='other')
    seed.vm_acl(HOST, VM_A, ['scoped'])

    response = api.as_user(user).post(f'/api/hyperv/{HOST}/vms/{VM_B}/preflight', json={})
    assert response.status_code == 403


# ===========================================================================
# The cluster list, against the real adapter
# ===========================================================================
#
# Every test above fakes the manager with a MagicMock, which answers any attribute asked
# of it. That is right for the routes under test and wrong for this one: the cluster list
# reads a manager the way upstream code does, and a MagicMock would satisfy a field the
# real adapter does not have. This missed exactly that once — /api/clusters returned a 500
# on `last_run` while the suite was green — so this drives the real class.

class TestTheClusterListRendersWithAHyperVHostRegistered:
    def _real_adapter(self, api):
        from pegaprox.core.hyperv_cluster import HyperVClusterManager

        class StubReader:
            def host_facts(self):
                return {'os_caption': 'Windows Server 2022', 'powershell_version': '5.1'}

            def verify_properties(self):
                return {'complete': True, 'missing': {}, 'inspected_vm': GUID_A}

            def list_vms(self):
                return []

            def close(self):
                pass

        adapter = HyperVClusterManager(HOST, {
            'name': 'Lab Hyper-V', 'host': 'hyperv.example', 'user': 'svc',
            'pass': 'fixture-' + 'not-a-real-credential',
        }, manager=StubReader())
        adapter.connect()
        api.set_manager(HOST, adapter)
        return adapter

    def test_the_cluster_list_renders(self, api, seed):
        """A page that 500s is worse than a missing feature: nothing on it renders."""
        self._real_adapter(api)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).get('/api/clusters')
        assert response.status_code == 200, response.get_data(as_text=True)[:400]

    def test_the_host_appears_in_it_as_a_hyperv_cluster(self, api, seed):
        self._real_adapter(api)
        admin = seed.user('root', role='admin')

        rows = api.as_user(admin).get('/api/clusters').get_json()
        row = next(r for r in rows if r['id'] == HOST)
        assert row['cluster_type'] == 'hyperv'
        assert row['connected'] is True

    def test_a_disconnected_host_does_not_take_the_list_down_with_it(self, api, seed):
        """A source that is switched off has to show as offline, not stop the page."""
        from pegaprox.core.hyperv_cluster import HyperVClusterManager
        from pegaprox.core.hyperv_errors import HyperVError

        class DeadReader:
            def host_facts(self):
                raise HyperVError('No route to host', kind=KIND_UNREACHABLE)

            def verify_properties(self):
                return {}

            def close(self):
                pass

        adapter = HyperVClusterManager(HOST, {'name': 'Lab', 'host': 'hyperv.example',
                                              'user': 'svc', 'pass': 'x'},
                                       manager=DeadReader())
        adapter.connect()
        api.set_manager(HOST, adapter)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).get('/api/clusters')
        assert response.status_code == 200
        row = next(r for r in response.get_json() if r['id'] == HOST)
        assert row['connected'] is False
        assert row['last_run'] is None


class TestNoClusterPageCrashesOnAHyperVHost:
    """Every cluster-scoped read that goes through the facade must not return a 500.

    A Hyper-V host appears in the sidebar like any other cluster, so opening it loads the
    overview, the pools, the networks and the storage list — none of which a migration
    source has. The failure mode this guards against is not a missing feature but a page
    that does not render at all, and it is the one the ESXi facade already has.

    Found by driving the real UI: four attributes and methods the suite did not miss were
    read unguarded, and each produced a 500 on a page the operator had opened for an
    unrelated reason.

    Which routes are in scope is read out of the routes themselves rather than listed by
    hand. A route that reaches the manager only through its methods is asking the facade a
    question, and this patch owes it an answer. A route that builds a Proxmox API URL and
    calls `_create_session` speaks to a product that is not there — SDN, Ceph, the
    datacenter firewall — and answering it would mean either faking a Proxmox or rewriting
    twenty upstream routes. Those are asserted not to raise, which is all a facade can
    promise: XCP-ng, which upstream ships, fails them in exactly the same way.
    """

    CLUSTER_SCOPED_GET = 'a GET route under /api/clusters/<cluster_id>/'

    # Reaching for any of these means the route has left the facade and is talking to a
    # Proxmox API directly.
    _PROXMOX_API_MARKERS = ('_create_session', '_api_get', '_api_post', 'api2/json')

    # The one facade route whose contract turns a correct answer into a 500: it reports
    # "no fingerprint" as a failure, the same way it does when a Proxmox cluster cannot be
    # read. The body says why; the status code is upstream's, and the fork does not change
    # it for a page the Hyper-V UI never opens.
    _ANSWERED_AS_AN_ERROR = ('/api/clusters/<cluster_id>/fingerprint',)

    def _real_adapter(self, api):
        from pegaprox.core.hyperv_cluster import HyperVClusterManager

        class StubReader:
            def host_facts(self):
                return {'os_caption': 'Windows Server 2022', 'powershell_version': '5.1'}

            def verify_properties(self):
                return {'complete': True, 'missing': {}, 'inspected_vm': GUID_A}

            def list_vms(self):
                return [{'guid': GUID_A, 'name': 'guest-a', 'state': 'Off', 'generation': 2,
                         'cpu_count': 2, 'memory_startup_bytes': 4294967296,
                         'memory_mb': 4096}]

            def get_vm(self, guid):
                return _vm_detail()

            def close(self):
                pass

        adapter = HyperVClusterManager(HOST, {
            'name': 'Lab Hyper-V', 'host': 'hyperv.example', 'user': 'svc',
            'pass': 'fixture-' + 'not-a-real-credential',
        }, manager=StubReader())
        adapter.connect()
        api.set_manager(HOST, adapter)
        return adapter

    def _top_level_cluster_routes(self, app):
        """The reads a browser issues on opening a cluster: no node, no VM, no sub-object.

        Deeper routes need an object that does not exist on this host anyway, and a 404
        from them is the right answer rather than something to assert here.
        """
        routes = []
        for rule in app.url_map.iter_rules():
            path = str(rule.rule)
            if not path.startswith('/api/clusters/<cluster_id>/'):
                continue
            if 'GET' not in rule.methods:
                continue
            tail = path[len('/api/clusters/<cluster_id>/'):]
            if '<' in tail:
                continue
            routes.append((path, rule.endpoint))
        return sorted(set(routes))

    def _speaks_proxmox_directly(self, app, endpoint):
        """Whether this route builds its own Proxmox request instead of asking the facade.

        Read out of the view function's source, so a route that stops doing it — or a new
        one that starts — moves between the two groups on its own.
        """
        import inspect

        view = app.view_functions[endpoint]
        sources = [self._source_of(view)]
        # Several routes do the Proxmox call in a shared helper in their own module, so
        # one level of indirection is followed. Deeper than that, and the route is doing
        # something this rule should not be guessing about.
        module = inspect.getmodule(view)
        for name, value in vars(module or object).items():
            if inspect.isfunction(value) and value is not view and f'{name}(' in sources[0]:
                sources.append(self._source_of(value))
        return any(marker in source
                   for source in sources for marker in self._PROXMOX_API_MARKERS)

    @staticmethod
    def _source_of(function):
        import inspect

        try:
            return inspect.getsource(function)
        except (OSError, TypeError):  # pragma: no cover — no source file for it
            return ''

    def test_the_enumeration_finds_the_pages_a_browser_opens(self, api):
        assert len(self._top_level_cluster_routes(api.app)) >= 8

    def test_the_split_puts_routes_on_both_sides(self, api):
        """Both groups are populated, so neither assertion is quietly testing nothing."""
        routes = self._top_level_cluster_routes(api.app)
        facade = [p for p, e in routes if not self._speaks_proxmox_directly(api.app, e)]
        proxmox = [p for p, e in routes if self._speaks_proxmox_directly(api.app, e)]
        assert len(facade) >= 8, facade
        assert len(proxmox) >= 8, proxmox

    def _read_every_route(self, api, seed):
        """Every top-level cluster read against the real adapter, with what came back.

        The rate-limit counter is cleared around the walk. It is process-wide and shared
        with every other test in the run, and one request per cluster-scoped route is a
        large enough share of it that spending it silently turns unrelated suites red.
        """
        import pegaprox.globals as ppglobals

        self._real_adapter(api)
        admin = seed.user('root', role='admin')
        client = api.as_user(admin)
        ppglobals.api_request_counts.clear()

        try:
            yield from self._walk(api, client)
        finally:
            ppglobals.api_request_counts.clear()

    def _walk(self, api, client):
        for path, endpoint in self._top_level_cluster_routes(api.app):
            # The test app re-raises instead of returning 500, so an escaping exception is
            # caught here and reported as what it would be in production: a page that does
            # not load. Catching it also lets one run name every gap at once.
            try:
                response = client.get(path.replace('<cluster_id>', HOST))
            except Exception as exc:  # noqa: BLE001 — any escape is the failure
                yield path, endpoint, None, f'{type(exc).__name__}: {exc}'
                continue
            body = response.get_data(as_text=True)[:160].replace('\n', ' ')
            yield path, endpoint, response.status_code, body

    def test_no_facade_route_returns_a_server_error(self, api, seed):
        crashed = []
        for path, endpoint, status, body in self._read_every_route(api, seed):
            if self._speaks_proxmox_directly(api.app, endpoint):
                continue
            if path in self._ANSWERED_AS_AN_ERROR:
                continue
            if status is None or status >= 500:
                crashed.append(f'{status or "raised"} {path} — {body}')

        assert not crashed, (
            'Cluster pages that crash on a Hyper-V host. Either answer the question on '
            'HyperVClusterManager, or branch on cluster_type in the route:\n  '
            + '\n  '.join(crashed))

    def test_no_proxmox_only_route_raises(self, api, seed):
        """The Proxmox-only pages may refuse, but they may not blow up.

        A caught failure renders as an error message on a page nobody opened for a Hyper-V
        host. An uncaught one is a traceback in the log on every poll, and it hides the
        failures worth reading.
        """
        raised = []
        for path, endpoint, status, body in self._read_every_route(api, seed):
            if not self._speaks_proxmox_directly(api.app, endpoint):
                continue
            if status is None:
                raised.append(f'{path} — {body}')

        assert not raised, (
            'Proxmox-only routes that raise instead of failing cleanly on a Hyper-V '
            'host:\n  ' + '\n  '.join(raised))

    def test_the_fingerprint_refusal_says_why(self, api, seed):
        """The one route answered as an error explains itself rather than just failing."""
        self._real_adapter(api)
        admin = seed.user('root', role='admin')
        response = api.as_user(admin).get(f'/api/clusters/{HOST}/fingerprint')

        assert response.status_code == 500
        assert 'migration source' in response.get_json()['error']


# ===========================================================================
# What a failed migration left, and removing it
# ===========================================================================

class TestTheMigrationRecordSurvivesTheProcess:
    """The shared migration list lives in a dict. This one answers from the database.

    A restart empties that dict while the half-built target it described is still there,
    which is the state an operator most needs to see.
    """

    def _record(self, migration_id='mig-a', guid=GUID_A, status='failed', leftovers=True):
        from pegaprox.core.db import get_db
        from pegaprox.core import hyperv_db

        conn = get_db().conn
        hyperv_db.create_migration(conn, source_cluster=HOST, source_vm_guid=guid,
                                   source_vm_name='guest-a', target_cluster='pve_1',
                                   target_node='node-a', migration_id=migration_id)
        if leftovers:
            hyperv_db.record_created_resource(conn, migration_id, 'vm', '120')
        hyperv_db.update_migration(conn, migration_id, status=status, target_vmid=120)
        return migration_id

    def test_a_failed_migration_is_listed_with_what_it_left(self, api, seed):
        fake = _hyperv_manager(api)
        fake.vmid_for.side_effect = lambda guid, name='': {GUID_A: VM_A, GUID_B: VM_B}[guid]
        self._record()
        admin = seed.user('root', role='admin')

        body = api.as_user(admin).get(f'/api/hyperv/{HOST}/migrations').get_json()

        row = body['migrations'][0]
        assert row['migration_id'] == 'mig-a'
        assert row['status'] == 'failed'
        assert [r['id'] for r in row['leftovers']] == ['120']
        assert row['blocks_retry'] is True

    def test_a_completed_migration_does_not_block_a_retry(self, api, seed):
        fake = _hyperv_manager(api)
        fake.vmid_for.side_effect = lambda guid, name='': {GUID_A: VM_A, GUID_B: VM_B}[guid]
        self._record(status='completed')
        admin = seed.user('root', role='admin')

        body = api.as_user(admin).get(f'/api/hyperv/{HOST}/migrations').get_json()
        assert body['migrations'][0]['blocks_retry'] is False

    def test_a_scoped_caller_is_not_told_about_other_vms_migrations(self, api, seed):
        """Reaching the host through one VM must not hand back the other VM's history,
        which names its target cluster, node and VMID."""
        fake = _hyperv_manager(api)
        fake.vmid_for.side_effect = lambda guid, name='': {GUID_A: VM_A, GUID_B: VM_B}[guid]
        self._record('mig-a', guid=GUID_A)
        self._record('mig-b', guid=GUID_B)

        seed.tenant('other', clusters=[])
        user = seed.user('scoped', role='user', tenant_id='other')
        seed.vm_acl(HOST, VM_A, ['scoped'])

        body = api.as_user(user).get(f'/api/hyperv/{HOST}/migrations').get_json()
        assert [row['migration_id'] for row in body['migrations']] == ['mig-a']


class TestCleanupIsRefusedUnlessEverythingAgrees:
    def _record(self, api, migration_id='mig-a'):
        from pegaprox.core.db import get_db
        from pegaprox.core import hyperv_db

        conn = get_db().conn
        hyperv_db.create_migration(conn, source_cluster=HOST, source_vm_guid=GUID_A,
                                   target_cluster='pve_1', target_node='node-a',
                                   migration_id=migration_id)
        hyperv_db.record_created_resource(conn, migration_id, 'vm', '120')
        hyperv_db.update_migration(conn, migration_id, status='failed', target_vmid=120)
        return migration_id

    def _host(self, api):
        fake = _hyperv_manager(api)
        fake.vmid_for.side_effect = lambda guid, name='': {GUID_A: VM_A, GUID_B: VM_B}[guid]
        return fake

    def test_without_a_matching_confirmation_it_refuses_and_says_what_is_there(self, api, seed):
        self._host(api)
        mid = self._record(api)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/cleanup', json={})

        assert response.status_code == 400
        assert [r['id'] for r in response.get_json()['leftovers']] == ['120']

    def test_a_confirmation_for_a_different_migration_does_not_count(self, api, seed):
        """A click on the wrong row must not delete a VM."""
        self._host(api)
        mid = self._record(api)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/cleanup',
            json={'confirm': 'some-other-migration'})

        assert response.status_code == 400

    def test_a_migration_of_another_host_is_not_reachable_here(self, api, seed):
        from pegaprox.core.db import get_db
        from pegaprox.core import hyperv_db

        self._host(api)
        hyperv_db.create_migration(get_db().conn, source_cluster='another_host',
                                   source_vm_guid=GUID_A, migration_id='mig-elsewhere')
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/mig-elsewhere/cleanup',
            json={'confirm': 'mig-elsewhere'})

        assert response.status_code == 404

    def test_a_caller_without_migrate_rights_on_the_vm_is_refused(self, api, seed):
        self._host(api)
        mid = self._record(api)
        seed.tenant('other', clusters=[])
        user = seed.user('scoped', role='user', tenant_id='other')
        # Reaches the host and may see this VM, but holds no migrate verb on it.
        seed.vm_acl(HOST, VM_A, ['scoped'], inherit_role=False, permissions=['vm.view'])

        response = api.as_user(user).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/cleanup', json={'confirm': mid})

        assert response.status_code == 403
        # From the per-VM gate, not from the cluster gate: this caller does reach the host.
        assert 'vm.migrate' in response.get_json()['error']

    def test_an_unreachable_target_refuses_rather_than_deleting_blind(self, api, seed):
        """Nothing is verified when the target cannot be read, so nothing is deleted."""
        self._host(api)
        mid = self._record(api)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/cleanup', json={'confirm': mid})

        assert response.status_code == 409
        assert 'not connected' in response.get_json()['error']


class TestTheSourceIsNotStartedWhileItsCopyRuns:
    """The route refuses, not just the core: a direct POST must hit the same wall."""

    def test_starting_the_original_is_refused_while_the_import_runs(self, api, seed, monkeypatch):
        from pegaprox.core import hyperv_xhm

        fake = _hyperv_manager(api)
        monkeypatch.setattr(hyperv_xhm, 'refuse_source_start',
                            lambda cluster_id, vmid: 'The imported copy is running as 120.')
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/start')

        assert response.status_code == 409
        assert '120' in response.get_json()['error']
        fake.manager.start_vm.assert_not_called()

    def test_an_ordinary_start_still_works(self, api, seed, monkeypatch):
        from pegaprox.core import hyperv_xhm

        fake = _hyperv_manager(api)
        monkeypatch.setattr(hyperv_xhm, 'refuse_source_start', lambda cluster_id, vmid: None)
        fake.manager.start_vm.return_value = {'success': True}
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/{VM_A}/start')

        assert response.status_code == 200
        fake.manager.start_vm.assert_called_once_with(GUID_A)


class TestThePostImportButtonsActOnTheTargetNotTheSource:
    """The two buttons that come after an import, at the route level.

    What these guard is the confusion the route names invite. `/vms/<vmid>/iso` mounts a
    medium in the VM that is still running on Hyper-V; `/migrations/<id>/virtio-iso` mounts
    one in the copy on Proxmox. Sending the driver ISO to the first would put it in the
    customer's live guest and do nothing for the imported one.
    """

    def _record(self, api, migration_id='mig-done'):
        from pegaprox.core.db import get_db
        from pegaprox.core import hyperv_db

        conn = get_db().conn
        hyperv_db.create_migration(conn, source_cluster=HOST, source_vm_guid=GUID_A,
                                   target_cluster='pve_1', target_node='node-a',
                                   migration_id=migration_id)
        hyperv_db.update_migration(conn, migration_id, status='completed', target_vmid=120)
        return migration_id

    def _host(self, api):
        fake = _hyperv_manager(api)
        fake.vmid_for.side_effect = lambda guid, name='': {GUID_A: VM_A, GUID_B: VM_B}[guid]
        return fake

    def _target(self, api, migration_id):
        """A connected target holding the imported VM, marked as this migration's."""
        from tests.test_hyperv_postimport import FakeTarget, IMPORTED
        from pegaprox.core.hyperv_xhm import target_vm_description

        node = FakeTarget(dict(IMPORTED,
                               description=target_vm_description(migration_id, 'guest-a')))
        node.config_of = {'120': node.config_of.pop(next(iter(node.config_of)))}
        api.set_manager('pve_1', node)
        return node

    def test_the_profile_is_previewed_before_it_is_applied(self, api, seed):
        self._host(api)
        mid = self._record(api)
        node = self._target(api, mid)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/profile', json={})

        assert response.status_code == 400
        details = [c['detail'] for c in response.get_json()['preview']['changes']]
        assert any('scsi0' in detail for detail in details)
        assert node.posts == []

    def test_a_confirmation_for_a_different_migration_does_not_count(self, api, seed):
        self._host(api)
        mid = self._record(api)
        node = self._target(api, mid)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/profile',
            json={'confirm': 'some-other-migration'})

        assert response.status_code == 400
        assert node.posts == []

    def test_a_migration_of_another_host_is_not_reachable_here(self, api, seed):
        from pegaprox.core.db import get_db
        from pegaprox.core import hyperv_db

        self._host(api)
        hyperv_db.create_migration(get_db().conn, source_cluster='another_host',
                                   source_vm_guid=GUID_A, migration_id='mig-elsewhere')
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/mig-elsewhere/virtio-iso', json={})

        assert response.status_code == 404

    def test_a_caller_without_migrate_rights_on_the_vm_is_refused(self, api, seed):
        self._host(api)
        mid = self._record(api)
        seed.tenant('other', clusters=[])
        user = seed.user('scoped', role='user', tenant_id='other')
        seed.vm_acl(HOST, VM_A, ['scoped'], inherit_role=False, permissions=['vm.view'])

        response = api.as_user(user).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/virtio-iso', json={})

        assert response.status_code == 403
        assert 'vm.migrate' in response.get_json()['error']

    def test_an_unreachable_target_refuses_rather_than_guessing(self, api, seed):
        self._host(api)
        mid = self._record(api)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/migrations/{mid}/virtio-iso', json={})

        assert response.status_code == 409
        assert 'not connected' in response.get_json()['error']

    def test_an_anonymous_caller_reaches_none_of_them(self, api):
        self._host(api)
        mid = self._record(api)

        for path in ('post-import', 'virtio-iso', 'drivers', 'profile'):
            method = api.anon().get if path == 'post-import' else api.anon().post
            response = method(f'/api/hyperv/{HOST}/migrations/{mid}/{path}')
            assert response.status_code == 401, path


class TestRegisteringAHost:
    """The transport is the operator's to choose. The API stores what was chosen, hands it
    back without the password, and refuses only what pypsrp could not act on at all."""

    class _Adapter:
        is_connected = True
        connection_error = ''

    def _connect_that_accepts(self, monkeypatch):
        from pegaprox.core import hyperv_cluster
        monkeypatch.setattr(hyperv_cluster, 'connect_hyperv_source',
                            lambda host_id, data: (self._Adapter(), None))
        monkeypatch.setattr(hyperv_cluster, 'register_hyperv_source',
                            lambda host_id, record, managers: None)

    def test_an_http_host_with_basic_auth_is_stored_as_configured(self, api, seed, monkeypatch):
        self._connect_that_accepts(monkeypatch)
        admin = seed.user('root', role='admin')

        created = api.as_user(admin).post('/api/hyperv/hosts', json={
            'name': 'plain', 'host': 'hyperv.example', 'user': 'svc',
            'pass': 'fixture-' + 'not-a-real-credential',
            'use_ssl': False, 'auth': 'basic', 'encrypt_messages': False,
        })
        assert created.status_code == 201, created.get_data(as_text=True)[:400]

        listed = api.as_user(admin).get('/api/hyperv/hosts').get_json()['hosts']
        row = next(h for h in listed if h['id'] == created.get_json()['id'])
        assert row['use_ssl'] is False
        assert row['port'] == 5985
        assert row['auth'] == 'basic'
        assert row['encrypt_messages'] is False
        assert row['has_password'] is True
        assert 'pass' not in row

    def test_an_https_host_without_a_port_lands_on_5986(self, api, seed, monkeypatch):
        self._connect_that_accepts(monkeypatch)
        admin = seed.user('root', role='admin')

        created = api.as_user(admin).post('/api/hyperv/hosts', json={
            'name': 'tls', 'host': 'hyperv.example', 'user': 'svc',
            'pass': 'fixture-' + 'not-a-real-credential', 'use_ssl': True,
        })
        assert created.status_code == 201, created.get_data(as_text=True)[:400]
        listed = api.as_user(admin).get('/api/hyperv/hosts').get_json()['hosts']
        row = next(h for h in listed if h['id'] == created.get_json()['id'])
        assert row['use_ssl'] is True
        assert row['port'] == 5986

    def test_an_unknown_authentication_method_is_refused_before_the_host_is_touched(
            self, api, seed, monkeypatch):
        from pegaprox.core import hyperv_cluster

        def must_not_connect(host_id, data):
            raise AssertionError('the connection test ran for a request that was invalid')

        monkeypatch.setattr(hyperv_cluster, 'connect_hyperv_source', must_not_connect)
        admin = seed.user('root', role='admin')

        response = api.as_user(admin).post('/api/hyperv/hosts', json={
            'name': 'x', 'host': 'hyperv.example', 'user': 'svc', 'pass': 'x', 'auth': 'digest',
        })
        assert response.status_code == 400
        assert 'negotiate' in response.get_json()['error']
