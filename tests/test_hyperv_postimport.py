# What happens to an imported VM after it arrives: the driver ISO, and the switch to the
# standard hardware.
#
# The distinction the whole feature turns on is tested here rather than described: an
# attached ISO is not an installed driver. A VM whose ISO is in the drive and whose drivers
# nobody confirmed must not be switched to VirtIO, because the guest would boot to a stop
# code and the only way back is a person at a console.
#
# The target is faked, so these prove the decisions. That the resulting configuration boots
# is proved on a real node.

import pytest

from pegaprox.core import hyperv_db, hyperv_postimport as postimport
from pegaprox.globals import cluster_managers

TARGET = 'pve_1'
SOURCE = 'hv_1'
GUID = '22222222-2222-2222-2222-222222222222'
NODE = 'node-a'
VMID = 132
ISO = 'iso-store:iso/virtio-win-0.1.271.iso'


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeTarget:
    """A node that holds one VM's config and lets a test watch it change.

    It keeps two views of that config, because Proxmox does. `config_of` is what the VM is
    configured as, pending changes included -- the default answer. `live_of` is what QEMU
    is actually running, which is what `current=1` asks for. While a VM runs the two come
    apart: changing a device that already exists takes effect at once, adding one that did
    not exist waits for the next start. A fake without that difference reports a CD in a
    guest that has no CD drive.
    """

    cluster_type = 'proxmox'

    def __init__(self, config, *, running=False, isos=(ISO,), storages=None):
        self.id = TARGET
        self.is_connected = True
        self.host = 'target-host.invalid'
        self.api_port = 8006
        self.config_of = {str(VMID): dict(config)}
        self.live_of = {str(VMID): dict(config)}
        self.running = running
        self.isos = list(isos)
        self.storages = storages if storages is not None else [
            {'storage': 'iso-store', 'content': 'iso,vztmpl'},
            {'storage': 'vmstorage', 'content': 'images,rootdir'},
        ]
        self.posts = []
        self.refuse_config = False

    def _api_get(self, url):
        if url.endswith('/status/current'):
            return FakeResponse(payload={'data': {
                'status': 'running' if self.running else 'stopped'}})
        if url.endswith(f'/nodes/{NODE}/storage'):
            return FakeResponse(payload={'data': self.storages})
        if '/storage/' in url and 'content=iso' in url:
            name = url.split('/storage/')[1].split('/')[0]
            return FakeResponse(payload={'data': [
                {'volid': volid, 'size': 500 * 1024 ** 2}
                for volid in self.isos if volid.startswith(f'{name}:')]})
        if '/config' in url:
            vmid = url.rsplit('/qemu/', 1)[-1].split('/')[0]
            live = url.endswith('current=1')
            source = self.live_of if live else self.config_of
            if self.config_of.get(vmid) is None:
                return FakeResponse(status_code=404)
            return FakeResponse(payload={'data': dict(source.get(vmid) or {})})
        return FakeResponse(status_code=404)

    def _api_post(self, url, data=None):
        payload = dict(data or {})
        self.posts.append((url, payload))
        if self.refuse_config:
            return FakeResponse(status_code=500, text='{"message":"VM is locked"}')
        vmid = url.rsplit('/qemu/', 1)[-1].split('/')[0]
        config = self.config_of.setdefault(vmid, {})
        live = self.live_of.setdefault(vmid, {})
        for key in (payload.pop('delete', '') or '').split(','):
            config.pop(key.strip(), None)
            if not self.running or key.strip() in live:
                live.pop(key.strip(), None)
        config.update(payload)
        for key, value in payload.items():
            # A device that already exists can be changed under a running VM; one that does
            # not exist yet is added to the pending configuration and appears at next start.
            if not self.running or key in live:
                live[key] = value
        return FakeResponse(status_code=200)


IMPORTED = {
    'name': 'guest',
    'bios': 'seabios',
    'machine': 'pc',
    'boot': 'order=sata0',
    'sata0': 'vmstorage:vm-132-disk-0,size=10G',
    'net0': 'e1000=00:15:5D:63:32:01,bridge=vmbr0',
    'memory': '4096',
}


@pytest.fixture
def migration(db):
    """One completed migration, with the mark its target VM carries."""
    conn = db.conn
    mid = hyperv_db.create_migration(
        conn, source_cluster=SOURCE, source_vm_guid=GUID, source_vm_name='guest',
        target_cluster=TARGET, target_node=NODE, target_storage='vmstorage')
    hyperv_db.update_migration(conn, mid, target_vmid=VMID,
                               status=hyperv_db.STATUS_COMPLETED, phase='completed')
    return mid


@pytest.fixture
def target(migration):
    """The node, wired in the way the product finds it."""
    from pegaprox.core.hyperv_xhm import target_vm_description
    config = dict(IMPORTED, description=target_vm_description(migration, 'guest'))
    node = FakeTarget(config)
    cluster_managers[TARGET] = node
    try:
        yield node
    finally:
        cluster_managers.pop(TARGET, None)


def _config(target):
    return target.config_of[str(VMID)]


# ---------------------------------------------------------------------------
# Whose VM is it
# ---------------------------------------------------------------------------

class TestOwnership:
    def test_a_vm_without_this_migrations_mark_is_left_alone(self, migration, target):
        """A VMID says nothing about who owns it; the description does."""
        _config(target)['description'] = 'somebody else moved in here'

        result = postimport.attach_virtio_iso(migration)

        assert result['success'] is False
        assert 'mark' in result['error']
        assert target.posts == []

    def test_a_vm_that_is_gone_is_reported_as_gone(self, migration, target):
        target.config_of.clear()

        result = postimport.attach_virtio_iso(migration)

        assert result['success'] is False
        assert 'no longer exists' in result['error']

    def test_a_migration_that_never_created_a_vm_has_nothing_to_change(self, db, target):
        mid = hyperv_db.create_migration(db.conn, source_cluster=SOURCE,
                                         source_vm_guid=GUID, target_cluster=TARGET)

        result = postimport.attach_virtio_iso(mid)

        assert result['success'] is False
        assert 'never created a VM' in result['error']


# ---------------------------------------------------------------------------
# The driver ISO
# ---------------------------------------------------------------------------

class TestAttachingTheIso:
    def test_the_iso_is_attached_as_a_cdrom_and_the_guest_is_not_touched(
            self, migration, target):
        result = postimport.attach_virtio_iso(migration)

        assert result['success'] is True
        assert _config(target)[postimport.VIRTIO_DRIVE] == f'{ISO},media=cdrom'
        # The disk the guest boots from is exactly where the import left it.
        assert _config(target)['sata0'] == IMPORTED['sata0']
        assert _config(target)['boot'] == 'order=sata0'

    def test_the_iso_is_found_without_being_named(self, migration, target):
        target.isos = ['iso-store:iso/debian-13.iso',
                       'iso-store:iso/virtio-win-0.1.271.iso']

        result = postimport.attach_virtio_iso(migration)

        assert result['iso'] == 'iso-store:iso/virtio-win-0.1.271.iso'

    def test_a_node_without_the_iso_says_so_instead_of_downloading_one(
            self, migration, target):
        target.isos = ['iso-store:iso/debian-13.iso']

        result = postimport.attach_virtio_iso(migration)

        assert result['success'] is False
        assert 'not download' in result['error']

    def test_a_medium_that_is_already_in_the_drive_is_not_displaced(self, migration, target):
        _config(target)[postimport.VIRTIO_DRIVE] = 'iso-store:iso/setup.iso,media=cdrom'

        result = postimport.attach_virtio_iso(migration)

        assert result['success'] is False
        assert 'setup.iso' in result['current']
        assert _config(target)[postimport.VIRTIO_DRIVE].endswith('setup.iso,media=cdrom')

    def test_replacing_it_is_possible_when_it_is_said_explicitly(self, migration, target):
        _config(target)[postimport.VIRTIO_DRIVE] = 'iso-store:iso/setup.iso,media=cdrom'

        result = postimport.attach_virtio_iso(migration, replace=True)

        assert result['success'] is True and result['replaced'] is True
        assert _config(target)[postimport.VIRTIO_DRIVE] == f'{ISO},media=cdrom'

    def test_an_empty_drive_is_not_a_medium(self, migration, target):
        _config(target)[postimport.VIRTIO_DRIVE] = 'none,media=cdrom'

        assert postimport.attach_virtio_iso(migration)['success'] is True


# ---------------------------------------------------------------------------
# The two facts that are not the same fact
# ---------------------------------------------------------------------------

class TestDriverState:
    def test_an_attached_iso_is_not_an_installed_driver(self, migration, target):
        postimport.attach_virtio_iso(migration)

        state = postimport.describe_driver_state(migration)

        assert state['iso_attached'] is True
        assert state['drivers_confirmed'] is False

    def test_a_confirmation_is_recorded_with_who_made_it(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')

        state = postimport.describe_driver_state(migration)
        assert state['drivers_confirmed'] is True
        assert state['confirmed_by'] == 'operator'
        assert state['confirmed_at']

    def test_a_confirmation_can_be_taken_back(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')

        postimport.confirm_drivers(migration, by='operator', confirmed=False)

        assert postimport.describe_driver_state(migration)['drivers_confirmed'] is False

    def test_the_confirmation_survives_the_profile_being_applied(self, migration, target):
        """The two post-import facts are independent and must not overwrite each other."""
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        row = hyperv_db.get_migration(postimport._conn(), migration)
        assert row['post_import']['drivers']['by'] == 'operator'
        assert row['post_import']['profile']['name'] == 'virtio'


# ---------------------------------------------------------------------------
# The standard profile
# ---------------------------------------------------------------------------

class TestPreview:
    def test_the_differences_are_shown_before_anything_is_changed(self, migration, target):
        preview = postimport.preview_profile(migration)

        details = ' '.join(change['detail'] for change in preview['changes'])
        assert 'sata0 to scsi0' in details
        assert 'e1000 to virtio' in details
        assert target.posts == []

    def test_missing_drivers_are_named_as_the_reason_it_cannot_run(self, migration, target):
        preview = postimport.preview_profile(migration)

        assert preview['can_apply'] is False
        assert any('confirmed' in requirement for requirement in preview['requirements'])

    def test_a_running_vm_is_named_and_not_powered_off(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')
        target.running = True

        preview = postimport.preview_profile(migration)

        assert preview['can_apply'] is False
        assert any('powered off' in requirement for requirement in preview['requirements'])
        assert target.posts == []

    def test_a_vm_already_on_the_profile_has_nothing_to_change(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')
        postimport.apply_profile(migration, confirmed=True)

        preview = postimport.preview_profile(migration)

        assert preview['already_applied'] is True
        assert preview['changes'] == []


class TestApplying:
    def test_nothing_happens_without_a_confirmation(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')

        result = postimport.apply_profile(migration)

        assert result['success'] is False
        assert result['preview']['changes']
        assert target.posts == []

    def test_a_guest_whose_drivers_were_never_confirmed_is_not_switched(
            self, migration, target):
        """The failure this refusal prevents: a Windows guest that cannot find its disk."""
        postimport.attach_virtio_iso(migration)

        result = postimport.apply_profile(migration, confirmed=True)

        assert result['success'] is False
        assert _config(target)['sata0'] == IMPORTED['sata0']
        assert 'scsi0' not in _config(target)

    def test_a_running_vm_is_never_powered_off_to_make_the_change(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')
        target.running = True

        result = postimport.apply_profile(migration, confirmed=True)

        assert result['success'] is False
        assert target.posts == []

    def test_the_disk_moves_to_the_virtio_controller_in_one_step(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')

        result = postimport.apply_profile(migration, confirmed=True)

        assert result['success'] is True
        config = _config(target)
        assert config['scsi0'].startswith('vmstorage:vm-132-disk-0')
        assert 'sata0' not in config
        assert config['boot'] == 'order=scsi0'
        assert config['scsihw'] == 'virtio-scsi-single'
        # One request, so the volume is never both detached and unattached.
        assert len(target.posts) == 1

    def test_the_card_changes_model_and_keeps_its_address(self, migration, target):
        """A changed MAC is a new machine to a DHCP server and to every licence check."""
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        assert _config(target)['net0'] == 'virtio=00:15:5D:63:32:01,bridge=vmbr0'

    def test_everything_outside_the_profile_is_left_as_it_was(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        config = _config(target)
        assert config['name'] == 'guest'
        assert config['memory'] == '4096'
        assert config['bios'] == 'seabios'
        assert config['machine'] == 'pc'

    def test_the_size_is_not_carried_to_the_new_key(self, migration, target):
        """Proxmox derives a disk's size from the volume; repeating it here is refused."""
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        assert 'size=' not in _config(target)['scsi0']

    def test_a_refused_change_is_reported_rather_than_assumed(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')
        target.refuse_config = True

        result = postimport.apply_profile(migration, confirmed=True)

        assert result['success'] is False
        assert 'locked' in result['error']

    def test_an_operator_who_knows_better_can_override_the_driver_gate(
            self, migration, target):
        """A guest imported with the drivers already in it never needed the ISO."""
        result = postimport.apply_profile(migration, confirmed=True, force=True)

        assert result['success'] is True
        assert _config(target)['scsi0'].startswith('vmstorage:vm-132-disk-0')

    def test_the_cdrom_is_not_treated_as_a_disk_to_move(self, migration, target):
        postimport.attach_virtio_iso(migration)
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        config = _config(target)
        assert config[postimport.VIRTIO_DRIVE] == f'{ISO},media=cdrom'
        assert config['boot'] == 'order=scsi0'


class TestTheProfileItself:
    def test_the_profile_is_what_the_manual_procedure_sets(self):
        """These are not preferences. They are the values the migration runbook has been
        setting by hand on every VM, so the button has to produce the same machine."""
        profile = postimport.STANDARD_PROFILE
        assert profile['controller'] == 'scsi'
        assert profile['nic_model'] == 'virtio'
        assert profile['disk_options'] == {'cache': 'writeback', 'discard': 'on',
                                           'ssd': '1'}
        assert profile['extra'] == {'cpu': 'x86-64-v2-AES', 'numa': '1', 'balloon': '0',
                                    'agent': '1'}

    def test_the_profile_leaves_the_firmware_alone(self):
        """The runbook also names a machine type, a BIOS and a TPM. Those belong to the
        import, which knows the source's generation: moving an installed guest from SeaBIOS
        to OVMF does not boot it."""
        profile = postimport.STANDARD_PROFILE
        assert 'machine' not in profile['extra']
        assert 'bios' not in profile['extra']
        assert 'tpmstate0' not in profile['extra']

    def test_the_settings_reach_the_vm(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')

        result = postimport.apply_profile(migration, confirmed=True)

        assert result['success'] is True
        config = _config(target)
        assert config['cpu'] == 'x86-64-v2-AES'
        assert config['numa'] == '1'
        assert config['balloon'] == '0'
        assert config['agent'] == '1'

    def test_a_setting_that_already_says_so_is_not_a_change(self, migration, target):
        _config(target)['numa'] = '1'
        postimport.confirm_drivers(migration, by='operator')

        preview = postimport.preview_profile(migration)

        assert not any(change['from'] == 'numa' for change in preview['changes'])

    def test_an_agent_that_carries_its_own_settings_is_left_alone(self, migration, target):
        """Proxmox writes an enabled agent both as `1` and as `enabled=1,...`. The long
        form holds settings somebody chose, and a bare `1` would drop them."""
        _config(target)['agent'] = 'enabled=1,fstrim_cloned_disks=1'
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        assert _config(target)['agent'] == 'enabled=1,fstrim_cloned_disks=1'

    def test_a_moved_disk_gains_the_profiles_disk_options(self, migration, target):
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        scsi0 = _config(target)['scsi0']
        assert scsi0.startswith('vmstorage:vm-132-disk-0')
        assert 'cache=writeback' in scsi0
        assert 'discard=on' in scsi0
        assert 'ssd=1' in scsi0
        assert 'size=' not in scsi0

    def test_an_option_the_disk_already_carries_wins(self, migration, target):
        """Somebody set `cache=none` on this disk deliberately. A hardware switch is not
        the place to overrule them."""
        _config(target)['sata0'] = 'vmstorage:vm-132-disk-0,cache=none,size=10G'
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        scsi0 = _config(target)['scsi0']
        assert 'cache=none' in scsi0
        assert 'cache=writeback' not in scsi0

    def test_a_disk_already_on_the_controller_still_gains_the_options(
            self, migration, target):
        """A VM switched before these values existed is not on the profile yet, and the
        disk nobody has to move is where that is cheapest to notice."""
        config = _config(target)
        config['scsi0'] = config.pop('sata0')
        config['boot'] = 'order=scsi0'
        postimport.confirm_drivers(migration, by='operator')

        result = postimport.apply_profile(migration, confirmed=True)

        assert result['success'] is True
        scsi0 = _config(target)['scsi0']
        assert 'discard=on' in scsi0
        assert _config(target)['boot'] == 'order=scsi0'
        assert 'unused0' not in _config(target)

    def test_the_preview_says_what_a_disk_gains(self, migration, target):
        preview = postimport.preview_profile(migration)

        disk = next(c for c in preview['changes'] if c['kind'] == 'disk')
        assert 'gains' in disk['detail']
        assert 'discard=on' in disk['detail']

    def test_a_profile_can_carry_further_settings_when_they_are_decided(
            self, migration, target):
        """A profile is data, not this module's opinion: an operator's own can differ."""
        postimport.confirm_drivers(migration, by='operator')
        profile = dict(postimport.STANDARD_PROFILE, extra={'cpu': 'kvm64'})

        result = postimport.apply_profile(migration, profile, confirmed=True)

        assert result['success'] is True
        assert _config(target)['cpu'] == 'kvm64'


class TestTheBootOrder:
    """A VM boots from the disk it booted from, not from the first one that moved."""

    def test_a_second_disk_does_not_become_the_boot_disk(self, migration, target):
        _config(target).update({'sata1': 'vmstorage:vm-132-disk-1,size=50G',
                                'boot': 'order=sata1'})
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        config = _config(target)
        assert config['boot'] == 'order=scsi1'
        assert config['scsi0'].startswith('vmstorage:vm-132-disk-0')
        assert config['scsi1'].startswith('vmstorage:vm-132-disk-1')

    def test_an_entry_that_did_not_move_keeps_its_place(self, migration, target):
        _config(target)['boot'] = 'order=sata0;net0'
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        assert _config(target)['boot'] == 'order=scsi0;net0'

    def test_a_vm_with_no_recorded_order_gets_the_lowest_moved_disk(self, migration, target):
        del _config(target)['boot']
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        assert _config(target)['boot'] == 'order=scsi0'


class TestASlotThatIsAlreadyTaken:
    """A moved disk must not be given a key that already holds something."""

    def test_a_disk_already_on_the_target_controller_is_not_overwritten(
            self, migration, target):
        _config(target).update({'scsi0': 'vmstorage:vm-132-disk-9,size=50G',
                                'boot': 'order=sata0'})
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        config = _config(target)
        assert config['scsi0'].startswith('vmstorage:vm-132-disk-9')
        assert config['scsi1'].startswith('vmstorage:vm-132-disk-0')
        assert config['boot'] == 'order=scsi1'

    def test_a_cdrom_on_the_target_controller_holds_its_number_too(self, migration, target):
        _config(target)['scsi0'] = 'iso-store:iso/setup.iso,media=cdrom'
        postimport.confirm_drivers(migration, by='operator')

        postimport.apply_profile(migration, confirmed=True)

        config = _config(target)
        assert config['scsi0'].endswith('media=cdrom')
        assert config['scsi1'].startswith('vmstorage:vm-132-disk-0')

    def test_a_vm_that_disappears_after_the_preview_reports_why(self, migration, target):
        """The preview and the change are two calls; the VM can go between them."""
        postimport.confirm_drivers(migration, by='operator')
        target.config_of.clear()

        result = postimport.apply_profile(migration, confirmed=True)

        assert result['success'] is False
        assert 'no longer exists' in result['error']


class TestAdrivedAddedToARunningVm:
    """Measured against a real node: the guest saw no disc and the product said it did.

    Proxmox can swap the medium in a drive a VM already has, but a drive that did not exist
    when the VM started goes into the pending configuration. `qm config` shows it, the
    guest does not have it, and QEMU reports no such device. Telling somebody to install
    drivers from that disc sends them looking for something that is not there.
    """

    def test_the_guest_is_told_it_needs_a_restart_to_see_the_disc(self, migration, target):
        target.running = True

        result = postimport.attach_virtio_iso(migration)

        assert result['success'] is True
        assert result['pending'] is True
        assert 'stopped and started again' in result['message']

    def test_a_stopped_vm_gets_the_disc_straight_away(self, migration, target):
        result = postimport.attach_virtio_iso(migration)

        assert result['pending'] is False
        assert 'Install the drivers inside the guest' in result['message']

    def test_a_drive_that_already_exists_takes_the_medium_while_it_runs(
            self, migration, target):
        """An empty drive present at boot is the case that does work live."""
        _config(target)[postimport.VIRTIO_DRIVE] = 'none,media=cdrom'
        target.live_of[str(VMID)][postimport.VIRTIO_DRIVE] = 'none,media=cdrom'
        target.running = True

        result = postimport.attach_virtio_iso(migration, replace=True)

        assert result['pending'] is False

    def test_the_state_separates_configured_from_visible(self, migration, target):
        target.running = True
        postimport.attach_virtio_iso(migration)

        state = postimport.describe_driver_state(migration)

        assert state['iso_attached'] is True
        assert state['iso_pending'] is True
        assert state['drivers_confirmed'] is False
