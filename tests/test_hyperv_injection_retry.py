# Retrying the VirtIO driver injection on a migration that completed with errors.
#
# What these pin: a failed injection on a Windows guest is a finished migration with an
# error, not a success and not a Linux guest; the retry refuses to write into a disk whose
# VM runs or is somebody else's; and a retry that works puts the VM back on the VirtIO
# hardware it was meant to have, while one that fails leaves it on the controller it boots
# from.
#
# The node is faked. That the injection itself finds the Windows partition is proved in
# tests/test_v2p_virtio_injection.py, on a real loop device.

import pytest

from pegaprox.core import hyperv_db, hyperv_xhm
from pegaprox.globals import cluster_managers

from tests.test_hyperv_postimport import FakeTarget, NODE, SOURCE, TARGET, VMID, GUID

ISO = '/mnt/pve/iso-store/template/iso/virtio-win-0.1.271.iso'

#: What the import leaves when the injection failed and the disks moved to SATA.
FELL_BACK = {
    'name': 'guest',
    'bios': 'ovmf',
    'machine': 'q35',
    'scsihw': 'virtio-scsi-single',
    'boot': 'order=sata1',
    'sata0': 'vmstorage:vm-132-disk-0,size=100G',
    'sata1': 'vmstorage:vm-132-disk-1,size=800G',
    'ide2': 'iso-store:iso/virtio-win-0.1.271.iso,media=cdrom',
    'net0': 'e1000=00:15:5D:08:1E:61,bridge=vmbr1,tag=1006',
}


@pytest.fixture
def migration(db):
    """A migration whose injection failed, with the volumes and ISO it recorded."""
    conn = db.conn
    mid = hyperv_db.create_migration(
        conn, source_cluster=SOURCE, source_vm_guid=GUID, source_vm_name='guest',
        target_cluster=TARGET, target_node=NODE, target_storage='vmstorage')
    hyperv_db.record_created_resource(conn, mid, 'vm', str(VMID))
    for disk in ('vmstorage:vm-132-disk-0', 'vmstorage:vm-132-disk-1'):
        hyperv_db.record_created_resource(conn, mid, 'volume', disk)
    hyperv_db.update_migration(conn, mid, target_vmid=VMID, phase='completed',
                               status=hyperv_db.STATUS_COMPLETED_WITH_ERRORS,
                               error='The VirtIO drivers could not be injected')
    hyperv_db.set_post_import(conn, mid, injection={'failed': True, 'iso': ISO,
                                                    'reason': 'x', 'at': 0})
    return mid


@pytest.fixture
def target(migration):
    config = dict(FELL_BACK, description=hyperv_xhm.target_vm_description(migration, 'guest'))
    node = FakeTarget(config)
    cluster_managers[TARGET] = node
    try:
        yield node
    finally:
        cluster_managers.pop(TARGET, None)


def _config(target):
    return target.config_of[str(VMID)]


def _injection(monkeypatch, *, ok=True, lines=()):
    """Replace the node-side injection. Returns the list of runs it was asked for."""
    runs = []

    def fake(run, _target, vmid):
        runs.append({'vmid': vmid, 'iso': run.config.get('virtio_iso_path')})
        view = hyperv_xhm._InjectionView(run, vmid)
        for line in lines:
            view.log(line)
        return view, ok

    monkeypatch.setattr(hyperv_xhm, '_run_offline_injection', fake)
    return runs


def _retry(migration, **kw):
    return hyperv_xhm.retry_driver_injection(migration, 'root', background=False, **kw)


class TestWhatTheSourceSaidAboutTheGuest:
    def test_a_windows_image_on_a_disk_is_windows(self):
        assert hyperv_xhm.guest_is_windows([{'windows': True}], {}) is True

    def test_a_windows_volume_found_by_the_inspection_is_windows(self):
        inspection = {'inspected': True, 'disks': [{'volumes': [{'windows': True}]}]}
        assert hyperv_xhm.guest_is_windows([], inspection) is True

    def test_inspected_volumes_without_windows_are_not_windows(self):
        inspection = {'inspected': True, 'disks': [{'volumes': [{'windows': False}]}]}
        assert hyperv_xhm.guest_is_windows([{'windows': False}], inspection) is False

    def test_a_disk_nobody_could_read_is_not_an_answer(self):
        """Get-WindowsImage failing looks the same for Linux and for an unreadable disk."""
        assert hyperv_xhm.guest_is_windows([{'windows': False, 'windows_error': 'x'}],
                                           {}) is None
        assert hyperv_xhm.guest_is_windows(None, None) is None


class TestTheRetryIsRefused:
    def test_while_the_vm_runs(self, migration, target, monkeypatch):
        runs = _injection(monkeypatch)
        target.running = True

        result = _retry(migration)

        assert result['success'] is False
        assert 'shut it down' in result['error']
        assert runs == [] and target.posts == []

    def test_on_a_vm_that_is_not_this_migrations(self, migration, target, monkeypatch):
        runs = _injection(monkeypatch)
        _config(target)['description'] = 'somebody else moved in here'

        result = _retry(migration)

        assert result['success'] is False
        assert 'mark' in result['error']
        assert runs == []

    def test_on_a_migration_that_completed_cleanly(self, db, migration, target, monkeypatch):
        runs = _injection(monkeypatch)
        hyperv_db.update_migration(db.conn, migration, status=hyperv_db.STATUS_COMPLETED)

        result = _retry(migration)

        assert result['success'] is False
        assert runs == []

    def test_while_another_retry_of_it_runs(self, migration, target, monkeypatch):
        runs = _injection(monkeypatch)
        hyperv_xhm._retries_running.add(migration)
        try:
            result = _retry(migration)
        finally:
            hyperv_xhm._retries_running.discard(migration)

        assert result['success'] is False
        assert runs == []


class TestARetryThatWorks:
    def test_uses_the_iso_the_migration_was_given(self, migration, target, monkeypatch):
        runs = _injection(monkeypatch)

        _retry(migration)

        assert runs == [{'vmid': VMID, 'iso': ISO}]

    def test_puts_the_disks_back_on_virtio_and_keeps_the_boot_disk(self, migration, target,
                                                                   monkeypatch):
        _injection(monkeypatch)

        _retry(migration)

        config = _config(target)
        assert config['scsi0'] == 'vmstorage:vm-132-disk-0'
        assert config['scsi1'] == 'vmstorage:vm-132-disk-1'
        assert 'sata0' not in config and 'sata1' not in config
        # It booted from the second disk on SATA; it boots from the same disk on VirtIO.
        assert config['boot'] == 'order=scsi1'

    def test_leaves_the_driver_cd_where_it_is(self, migration, target, monkeypatch):
        _injection(monkeypatch)

        _retry(migration)

        assert _config(target)['ide2'] == FELL_BACK['ide2']

    def test_moves_the_nic_back_without_losing_its_mac_or_vlan(self, migration, target,
                                                               monkeypatch):
        _injection(monkeypatch)

        _retry(migration)

        assert _config(target)['net0'] == 'virtio=00:15:5D:08:1E:61,bridge=vmbr1,tag=1006'

    def test_reports_the_migration_as_completed_and_records_why(self, db, migration, target,
                                                                monkeypatch):
        _injection(monkeypatch)

        _retry(migration)

        row = hyperv_db.get_migration(db.conn, migration)
        assert row['status'] == hyperv_db.STATUS_COMPLETED
        assert not row['error']
        post = row['post_import']
        assert 'injection' not in post
        assert post['drivers']['confirmed'] is True
        assert 'offline injection' in post['drivers']['by']

    def test_does_not_start_the_vm(self, migration, target, monkeypatch):
        _injection(monkeypatch)

        _retry(migration)

        assert not [url for url, _ in target.posts if url.endswith('/status/start')]


class TestARetryThatFailsAgain:
    def test_changes_nothing_about_the_vm(self, migration, target, monkeypatch):
        _injection(monkeypatch, ok=False, lines=['WIN_PART=/dev/loop1p5', 'NO_WINDOWS_DIR'])

        _retry(migration)

        assert target.posts == []
        assert _config(target)['sata1'] == FELL_BACK['sata1']

    def test_stays_completed_with_errors_and_names_the_partition(self, db, migration, target,
                                                                 monkeypatch):
        _injection(monkeypatch, ok=False, lines=['WIN_PART=/dev/loop1p5', 'NO_WINDOWS_DIR'])

        _retry(migration)

        row = hyperv_db.get_migration(db.conn, migration)
        assert row['status'] == hyperv_db.STATUS_COMPLETED_WITH_ERRORS
        assert '/dev/loop1p5' in row['error']

    def test_can_be_retried_once_more(self, migration, target, monkeypatch):
        _injection(monkeypatch, ok=False)
        _retry(migration)

        assert migration not in hyperv_xhm._retries_running
        runs = _injection(monkeypatch)
        assert _retry(migration)['success'] is True
        assert len(runs) == 1


class TestTheRouteAsksFirst:
    def test_the_route_is_registered_as_a_target_side_write(self, api):
        rules = {(str(r.rule), m) for r in api.app.url_map.iter_rules() for m in r.methods}
        assert ('/api/hyperv/<cluster_id>/migrations/<migration_id>/retry-injection',
                'POST') in rules
