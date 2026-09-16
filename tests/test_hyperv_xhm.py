# The Hyper-V to Proxmox migration, driven without either hypervisor.
#
# The runner's shape is the thing under test: what it asks before doing anything
# irreversible, what it cleans up when a step fails, and what it deliberately leaves alone.
# Both ends are faked — the Hyper-V source answers from a dict, and the target node records
# the commands it was told to run instead of running them.
#
# The one thing this cannot prove is that the commands work. That is proved in the Docker
# testbed, where a real cifs mount and a real qemu-img conversion produce a raw image whose
# checksum matches the source VHDX.

import threading

import pytest

from pegaprox.core import hyperv_xhm

from pegaprox.core import hyperv_db, hyperv_xhm
from pegaprox.core.hyperv_transfer import TransferError

SOURCE = 'hv_1'
TARGET = 'pve_1'
VMID = 100
GUID = '11111111-1111-1111-1111-111111111111'
SECRET = 'fixture-' + 'not-a-real-credential'


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------

class FakeConfig:
    def __init__(self, **kw):
        self.name = kw.get('name', 'source')
        self.host = kw.get('host', 'source-host.invalid')
        self.user = kw.get('user', 'CORP\\svc-migrate')
        self.pass_ = kw.get('pass_', SECRET)
        self.smb_share_map = kw.get('smb_share_map', {})
        self.smb_domain = kw.get('smb_domain', '')
        self.transfer_host = kw.get('transfer_host', '')
        self.ssh_user = kw.get('ssh_user', 'root')
        self.ssh_key = ''
        self.ssh_port = 22


class FakeSource:
    cluster_type = 'hyperv'

    def __init__(self, detail, safe=(True, 'Off, no background operation')):
        self.id = SOURCE
        self.is_connected = True
        self.config = FakeConfig()
        self._detail = detail
        self._safe = safe
        self.manager = self

    @property
    def transfer_address(self):
        # The same fallback the real manager has: a host with no separate transfer path
        # is mounted at its management address.
        return (getattr(self.config, 'transfer_host', '') or '').strip() or self.config.host

    def guid_for(self, vmid):
        return GUID if int(vmid) == VMID else None

    def vm_detail(self, vmid):
        return dict(self._detail)

    def disks_are_safe_to_read(self, guid):
        return self._safe


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeTarget:
    cluster_type = 'proxmox'

    def __init__(self, free_bytes=10 * 1024 ** 4, next_id=120):
        self.id = TARGET
        self.is_connected = True
        self.host = 'target-host.invalid'
        self.api_port = 8006
        self.config = FakeConfig(name='target')
        self._free = free_bytes
        self._next_id = next_id
        self.posts = []
        # _get_pve_targets reads this to enumerate storages and bridges for the wizard.
        self.nodes = {'node-a': {}}

    def _api_get(self, url):
        if url.endswith('/nextid'):
            return FakeResponse(payload={'data': self._next_id})
        if '/storage/' in url:
            return FakeResponse(payload={'data': {'avail': self._free}})
        if url.endswith('/config') and self.vm_configs is not None:
            vmid = url.rsplit('/qemu/', 1)[-1].split('/')[0]
            config = self.vm_configs.get(vmid)
            if config is None:
                return FakeResponse(status_code=404)
            return FakeResponse(payload={'data': config})
        return FakeResponse(status_code=404)

    #: Posts whose URL contains one of these keys answer with the given status instead of
    #: 200, so a test can make one attach fail the way a locked VM does.
    refuse_posts = ()
    #: Set when the VM was created, so a test can assert the lock was waited out before
    #: anything was attached to it.
    waited_for = None

    def _api_post(self, url, data=None):
        payload = dict(data or {})
        self.posts.append((url, payload))
        for marker in self.refuse_posts:
            if marker in payload:
                return FakeResponse(status_code=500,
                                    text='{"message":"VM is locked (create)"}')
        if url.endswith('/qemu'):
            # Creating a VM is asynchronous on a real node and answers with a task id.
            return FakeResponse(payload={'data': 'UPID:node-a:0001:create::root@pam:'})
        return FakeResponse(status_code=200)

    def _wait_for_task(self, node, task_id, timeout=600):
        self.waited_for = (node, task_id)
        return True

    # -- what a cleanup asks of a target -------------------------------------------
    # `vm_configs` maps a VMID to what /config answers, so a test can put somebody
    # else's guest on the number a failed migration recorded.
    vm_configs = None

    def _api_delete(self, url):
        self.deleted.append(url)
        return FakeResponse(status_code=200)

    @property
    def deleted(self):
        if not hasattr(self, '_deleted'):
            self._deleted = []
        return self._deleted


class FakeNode:
    """Records commands instead of running them, and answers the few that are read."""

    def __init__(self, convert_exit=0, alloc_exit=0, probe_exit=0):
        self.commands = []
        self.stdin_data = []
        self.closed = False
        self.convert_exit = convert_exit
        self.alloc_exit = alloc_exit
        self.probe_exit = probe_exit
        self._alloc_count = 0

    def run(self, command, stdin_data=None, timeout=None):
        self.commands.append(command)
        if stdin_data is not None:
            self.stdin_data.append(stdin_data)
        if command.startswith('test -r'):
            return self.probe_exit, '42949672960\n', ''
        if command.startswith('pvesm alloc'):
            if self.alloc_exit != 0:
                return self.alloc_exit, '', 'no space left on device'
            index = self._alloc_count
            self._alloc_count += 1
            # What pvesm actually prints on an LVM-thin storage: a warning line, then the
            # volume in quotes. The runner has to find the volume in both shapes.
            return 0, ('  WARNING: Sum of all thin volume sizes exceeds the size of '
                       'thin pool.\n'
                       f"successfully created 'local-lvm:vm-120-disk-{index}'\n"), ''
        if command.startswith('pvesm path'):
            return 0, '/dev/pve/vm-120-disk-0\n', ''
        return 0, '', ''

    def run_with_progress(self, command, on_progress, cancelled, timeout=None):
        self.commands.append(command)
        on_progress(50.0)
        if self.convert_exit != 0:
            return self.convert_exit, '', 'qemu-img: error while reading sector'
        on_progress(100.0)
        return 0, '', ''

    def close(self):
        self.closed = True


class FakeTask:
    """The fields the runner touches on an XHMigrationTask."""

    def __init__(self, **kw):
        self.id = kw.get('id', 'mig12345')
        self.source_cluster = SOURCE
        self.target_cluster = TARGET
        self.source_vmid = VMID
        self.target_node = 'node-a'
        self.target_storage = 'local-lvm'
        self.vm_name = ''
        self.config = kw.get('config', {})
        self.network_map = kw.get('network_map', {})
        self.phase = 'planning'
        self.status = 'running'
        self.progress = 0
        self.error = None
        self.target_vmid = None
        self.disk_progress = {}
        self.log_lines = []
        self.cancel_event = threading.Event()

    def log(self, message):
        self.log_lines.append(message)

    def set_phase(self, phase, error=None):
        self.phase = phase
        if phase == 'failed':
            self.status = 'failed'
            self.error = error
        elif phase == 'completed':
            self.status = 'completed'

    def update_progress(self, key, copied, total):
        self.disk_progress[key] = {'copied': copied, 'total': total}


def _detail(**overrides):
    detail = {
        'guid': GUID, 'name': 'guest-a', 'state': 'Off', 'generation': 2,
        'cpu_count': 4, 'memory_mb': 8192, 'checkpoint_count': 0, 'checkpoints': [],
        'secure_boot_enabled': False, 'vtpm_enabled': False,
        'bitlocker_state': None, 'virtio_driver_state': None,
        'disks': [{'path': 'C:\\vm\\a.vhdx', 'size': 42949672960, 'vhd_type': 'Dynamic',
                   'parent_path': None, 'target_controller_hint': 'scsi',
                   'read_error': None}],
        # As normalise_nic produces it: Hyper-V reports a MAC without separators, and the
        # colon-separated spelling is derived alongside it. A fixture that carries only the
        # colon form hides which of the two the code under test actually reads.
        'network_adapters': [{'name': 'Network Adapter',
                              'mac_address': '00155D000001',
                              'mac_address_colons': '00:15:5d:00:00:01',
                              'switch_name': 'External'}],
    }
    detail.update(overrides)
    return detail


@pytest.fixture
def wired(db, monkeypatch):
    """Source, target and node in cluster_managers, with the SSH layer replaced."""
    import pegaprox.globals as ppglobals

    source = FakeSource(_detail())
    target = FakeTarget()
    node = FakeNode()

    ppglobals.cluster_managers.clear()
    ppglobals.cluster_managers[SOURCE] = source
    ppglobals.cluster_managers[TARGET] = target

    monkeypatch.setattr(hyperv_xhm, '_open_target_node',
                        lambda task, src, tgt: (node, '/mnt/pegaprox-hyperv/x.credentials'))
    # The retry backoff is real seconds in production and dead time here. Zeroing the
    # constant keeps the retry behaviour under test and the suite fast.
    monkeypatch.setattr(hyperv_xhm, '_RETRY_BACKOFF_SECONDS', 0)
    try:
        yield source, target, node
    finally:
        ppglobals.cluster_managers.clear()


# Every warning this fixture produces. A real run collects these from the operator; a test
# about the transfer should not fail because of a confirmation the wizard would have taken.
ACKNOWLEDGED = sorted(__import__('pegaprox.core.hyperv_preflight', fromlist=['x'])
                      ._ACKNOWLEDGEABLE_CHECKS)


def _run(task, network_map=None, acknowledged=None):
    # Keyed the way the runner keys it: by `mac_address`, which is Hyper-V's spelling
    # without separators, not by the colon-separated form the target is sent.
    task.network_map = network_map if network_map is not None else {
        '00155D000001': 'vmbr0'}
    task.config = {**task.config,
                   'acknowledged': ACKNOWLEDGED if acknowledged is None else acknowledged}
    hyperv_xhm._run_hyperv_to_pve(task)
    return task


# ===========================================================================
# Planning
# ===========================================================================

class TestPlanning:
    def test_a_source_that_is_not_hyperv_is_refused(self, db):
        import pegaprox.globals as ppglobals
        ppglobals.cluster_managers.clear()
        ppglobals.cluster_managers[TARGET] = FakeTarget()
        try:
            result = hyperv_xhm.plan_hyperv_to_pve(TARGET, VMID, TARGET)
        finally:
            ppglobals.cluster_managers.clear()
        assert 'error' in result

    def test_the_plan_carries_the_preflight_verdict(self, db, wired):
        """The other directions answer "here is what we found".

        This one also answers "here is why you cannot start yet", because a Hyper-V source
        has states that produce a corrupt copy rather than a failure.
        """
        source, _, _ = wired
        source.get_vm_disks_for_export = lambda vmid: {'data': {
            'name': 'guest-a', 'cpu_count': 4, 'memory_mb': 8192, 'generation': 2,
            'disks': [{'key': 'disk-0', 'capacity_bytes': 42949672960,
                       'target_controller_hint': 'scsi'}],
            'network_adapters': [], 'power_state': 'Off', 'checkpoint_count': 0,
            'hyperv_guid': GUID}}

        plan = hyperv_xhm.plan_hyperv_to_pve(SOURCE, VMID, TARGET)
        assert plan['direction'] == 'hyperv_to_pve'
        assert 'preflight' in plan
        assert plan['source']['bios'] == 'ovmf'
        assert plan['source']['machine'] == 'q35'

    def test_the_plan_names_each_adapter_by_the_key_the_mapping_uses(self, db, wired):
        """The wizard, the preflight and the runner must address one adapter alike.

        The migration wizard keys its network map by the `network` field of each plan
        entry; the preflight and the runner look the choice up by MAC. If those drift
        apart nothing fails visibly — the wizard writes under one key, the preflight reads
        another, every adapter stays "unmapped", and the migration can never be started.
        Found in the browser, where the start button stayed disabled with the form filled.
        """
        from pegaprox.core.hyperv_preflight import adapter_key, check_network_mapping

        adapter = {'name': 'Network Adapter', 'mac_address': '00:15:5d:00:00:01',
                   'switch_name': 'External'}
        source, _, _ = wired
        source.get_vm_disks_for_export = lambda vmid: {'data': {
            'disks': [], 'network_adapters': [adapter], 'generation': 2,
            'power_state': 'Off', 'checkpoint_count': 0, 'hyperv_guid': GUID}}

        plan = hyperv_xhm.plan_hyperv_to_pve(SOURCE, VMID, TARGET)
        entry = plan['source']['networks'][0]

        assert entry['network'] == adapter_key(adapter, 0)
        # And a map built the way the wizard builds it satisfies the check.
        chosen = {entry['network'] or entry['bridge'] or '0': 'vmbr0'}
        assert check_network_mapping([adapter], chosen).severity == 'ok'

    def test_an_adapter_row_is_labelled_with_something_a_person_recognises(self, db, wired):
        """The wizard labels the row with `bridge`; a MAC there tells nobody anything."""
        source, _, _ = wired
        source.get_vm_disks_for_export = lambda vmid: {'data': {
            'disks': [], 'generation': 1, 'hyperv_guid': GUID,
            'network_adapters': [{'name': 'Network Adapter', 'switch_name': 'External',
                                  'mac_address': '00:15:5d:00:00:01'}]}}

        entry = hyperv_xhm.plan_hyperv_to_pve(SOURCE, VMID, TARGET)['source']['networks'][0]
        assert entry['bridge'] == 'External'

    def test_the_plan_gives_no_time_estimate(self, db, wired):
        """The other directions divide bytes by a fixed rate and present the result as a
        number. Nothing here has measured the file share this one reads from."""
        source, _, _ = wired
        source.get_vm_disks_for_export = lambda vmid: {'data': {
            'disks': [], 'network_adapters': [], 'generation': 1, 'hyperv_guid': GUID}}
        plan = hyperv_xhm.plan_hyperv_to_pve(SOURCE, VMID, TARGET)
        assert 'estimated_seconds' not in plan
        assert 'total_bytes' in plan


# ===========================================================================
# What the run asks before it does anything
# ===========================================================================

class TestTheStartContract:
    def test_a_source_still_merging_its_disks_is_refused(self, db, wired):
        """Deleting a checkpoint returns at once and the merge runs on afterwards.

        A VM that reports Off with no checkpoints can still be rewriting its own disk
        files, and copying them then produces an image that mounts and is wrong.
        """
        source, _, node = wired
        source._safe = (False, 'Merging Disks')

        task = _run(FakeTask())
        assert task.status == 'failed'
        assert 'Merging Disks' in task.error
        assert 'pvesm alloc' not in ' '.join(node.commands)

    def test_preflight_runs_again_at_the_moment_of_starting(self, db, wired):
        """The wizard's verdict can be minutes old. A checkpoint taken in between is
        exactly the case this catches."""
        source, _, node = wired
        source._detail = _detail(checkpoint_count=2)

        task = _run(FakeTask())
        assert task.status == 'failed'
        assert 'checkpoint' in task.error.lower()
        assert not any(c.startswith('pvesm alloc') for c in node.commands)

    def test_an_unmapped_adapter_stops_the_run_rather_than_guessing(self, db, wired):
        """A VM that arrives on the wrong VLAN is reachable by the wrong people, and that
        is not visible from the migration's own result."""
        task = _run(FakeTask(), network_map={})
        assert task.status == 'failed'
        assert 'network' in task.error.lower()

    def test_an_unconfirmed_warning_stops_the_run(self, db, wired):
        """A warning nobody confirmed is a risk nobody accepted.

        The unbuilt transport proof is one of them, and it must not be possible to start a
        migration past it by calling the API directly.
        """
        task = _run(FakeTask(), acknowledged=[])
        assert task.status == 'failed'
        assert 'confirmed' in task.error

    def test_a_target_with_no_room_stops_before_anything_is_allocated(self, db, wired):
        _, target, node = wired
        target._free = 1024

        task = _run(FakeTask())
        assert task.status == 'failed'
        assert not any(c.startswith('pvesm alloc') for c in node.commands)


# ===========================================================================
# The transfer
# ===========================================================================

class TestTheTransfer:
    def test_a_whole_run_reaches_completed_and_leaves_the_source_alone(self, db, wired):
        source, target, node = wired
        task = _run(FakeTask())

        assert task.status == 'completed', task.error
        assert task.target_vmid == 120
        # Nothing was asked of the source but reading it.
        assert not hasattr(source, 'started')
        assert 'untouched' in ' '.join(task.log_lines)

    def test_the_share_credentials_never_appear_in_a_command(self, db, wired):
        _, _, node = wired
        _run(FakeTask())
        assert not any(SECRET in command for command in node.commands)

    def test_the_mount_is_undone_even_when_the_conversion_fails(self, db, wired):
        """A mount left behind holds a connection open to a customer's hypervisor."""
        _, _, node = wired
        node.convert_exit = 1

        task = _run(FakeTask())
        assert task.status == 'failed'
        assert any(c.startswith('umount') for c in node.commands)
        assert node.closed

    def test_a_failed_conversion_frees_its_partial_volume(self, db, wired):
        """qemu-img writes the target in whatever order the source's block table
        dictates, so a partial volume is not a partial disk anybody can resume."""
        _, _, node = wired
        node.convert_exit = 1

        _run(FakeTask())
        assert any(c.startswith('pvesm free') for c in node.commands)

    def test_a_conversion_is_retried_on_a_fresh_volume(self, db, wired):
        _, _, node = wired
        node.convert_exit = 1

        _run(FakeTask())
        allocs = [c for c in node.commands if c.startswith('pvesm alloc')]
        frees = [c for c in node.commands if c.startswith('pvesm free')]
        assert len(allocs) == hyperv_xhm.TRANSFER_ATTEMPTS
        assert len(frees) == hyperv_xhm.TRANSFER_ATTEMPTS

    def test_a_disk_missing_from_the_share_fails_before_allocating(self, db, wired):
        """The commonest failure is a share that mounted but does not hold what the
        inventory named. It must not cost a volume to discover."""
        _, _, node = wired
        node.probe_exit = 1

        task = _run(FakeTask())
        assert task.status == 'failed'
        assert not any(c.startswith('pvesm alloc') for c in node.commands)

    def test_the_allocation_is_rounded_up(self, db, wired):
        """pvesm alloc takes kibibytes and rounds down. A disk whose size is not a whole
        number of them would get a volume one write short, failing at the very end."""
        source, _, node = wired
        source._detail = _detail(disks=[{'path': 'C:\\vm\\a.vhdx', 'size': 1024 * 1024 + 1,
                                         'vhd_type': 'Fixed', 'parent_path': None,
                                         'target_controller_hint': 'scsi',
                                         'read_error': None}])
        _run(FakeTask())
        alloc = next(c for c in node.commands if c.startswith('pvesm alloc'))
        assert alloc.split()[-1] == '1025'

    def test_a_disk_with_no_size_is_refused(self, db, wired):
        source, _, node = wired
        source._detail = _detail(disks=[{'path': 'C:\\vm\\a.vhdx', 'size': None,
                                         'vhd_type': 'Dynamic', 'parent_path': None,
                                         'target_controller_hint': 'scsi',
                                         'read_error': None}])
        task = _run(FakeTask())
        assert task.status == 'failed'

    def test_cancelling_stops_before_the_next_disk(self, db, wired):
        task = FakeTask()
        task.cancel_event.set()
        _run(task)
        assert task.status == 'failed'
        assert 'ancel' in task.error


# ===========================================================================
# What lands on the target
# ===========================================================================

class TestTheTargetVm:
    def _created(self, target):
        return next(data for url, data in target.posts if url.endswith('/qemu'))

    def test_a_generation_two_source_becomes_a_uefi_q35_machine(self, db, wired):
        _, target, _ = wired
        _run(FakeTask())
        created = self._created(target)
        assert created['bios'] == 'ovmf'
        assert created['machine'] == 'q35'

    def test_a_generation_one_source_becomes_a_seabios_pc_machine(self, db, wired):
        """The i440fx machine is spelled 'pc' here; Proxmox refuses 'i440fx' itself.

        Measured against Proxmox VE 9.2.11: machine='i440fx' answers HTTP 400, 'pc' and
        'q35' are accepted. Getting this wrong failed every Generation 1 import.
        """
        source, target, _ = wired
        source._detail = _detail(generation=1)
        _run(FakeTask())
        created = self._created(target)
        assert created['bios'] == 'seabios'
        assert created['machine'] == 'pc'

    def test_a_uefi_guest_gets_somewhere_to_keep_its_variables(self, db, wired):
        """Without an EFI disk the VM starts into the firmware shell, which reads as a
        failed conversion rather than as a missing device."""
        _, target, _ = wired
        _run(FakeTask())
        assert any('efidisk0' in data for _, data in target.posts)

    def test_a_secure_boot_guest_gets_the_keys_it_was_booting_with(self, db, wired):
        """Proxmox ships an OVMF store with Microsoft's certificates enrolled, which is the
        same set the standard Hyper-V template holds. Handing such a guest an empty store
        means Secure Boot is simply off on the target — a different machine to anything
        that measures it."""
        source, target, _ = wired
        source._detail = _detail(generation=2, secure_boot_enabled=True)
        _run(FakeTask())
        efi = [data['efidisk0'] for _, data in target.posts if 'efidisk0' in data]
        assert efi, 'no EFI variable store was created'
        assert 'pre-enrolled-keys=1' in efi[0]

    def test_a_guest_without_secure_boot_does_not_get_keys_enrolled(self, db, wired):
        """Enrolling keys under a guest that was booting without Secure Boot can stop it
        booting at all: its loader or one of its drivers may be unsigned."""
        source, target, _ = wired
        source._detail = _detail(generation=2, secure_boot_enabled=False)
        _run(FakeTask())
        efi = [data['efidisk0'] for _, data in target.posts if 'efidisk0' in data]
        assert efi, 'no EFI variable store was created'
        assert 'pre-enrolled-keys=0' in efi[0]

    def test_a_secure_boot_state_the_host_did_not_report_enrols_nothing(self, db, wired):
        """None is not False. A Generation 2 guest whose state could not be read must not
        be handed keys under a loader that might be unsigned — nor an empty store passed
        off as "it had none"; the preflight warns about that case separately."""
        source, target, _ = wired
        source._detail = _detail(generation=2, secure_boot_enabled=None)
        _run(FakeTask())
        efi = [data['efidisk0'] for _, data in target.posts if 'efidisk0' in data]
        assert efi and 'pre-enrolled-keys=0' in efi[0]

    def test_the_mac_address_comes_across(self, db, wired):
        """Licence bindings, DHCP reservations and firewall rules are written against it.
        A new MAC turns a migration into a new machine for all of them."""
        _, target, _ = wired
        _run(FakeTask())
        created = self._created(target)
        assert '00:15:5d:00:00:01' in created['net0']

    def test_the_mac_is_sent_in_the_spelling_the_target_accepts(self, db, wired):
        """Hyper-V reports `00155D000001`; Proxmox refuses it in that form.

        Measured against a real node: "net0.macaddr: invalid format - value does not look
        like a valid unicast MAC address", which failed every VM that had an adapter.
        """
        _, target, _ = wired
        _run(FakeTask())
        assert 'macaddr=00:15:5d:00:00:01' in self._created(target)['net0']
        assert '00155D000001' not in self._created(target)['net0']

    def test_the_source_vlan_is_carried_to_the_target(self, db, wired):
        """A VLAN is not something a migration may quietly get wrong.

        An adapter that arrives on the wrong VLAN is reachable by the wrong people, and
        the guest looks healthy either way -- so the id the source reports is what the
        target gets, not a default.
        """
        source, target, _ = wired
        source._detail['network_adapters'] = [{'name': 'Network Adapter',
                                               'mac_address': '00155D000001',
                                               'mac_address_colons': '00:15:5d:00:00:01',
                                               'switch_name': 'External',
                                               'vlan_mode': 'Access', 'vlan_id': 22}]
        _run(FakeTask())
        assert 'tag=22' in self._created(target)['net0']

    def test_an_adapter_without_a_vlan_gets_the_configured_default(self, db, wired):
        """Hyper-V leaves an adapter untagged whenever the switch port does the tagging.

        "No VLAN on the source" therefore usually means "the operator knows which one",
        not "untagged" -- so the fallback applies instead of putting the guest on whatever
        the target bridge's native VLAN happens to be.
        """
        source, target, _ = wired
        source._detail['network_adapters'] = [{'name': 'Network Adapter',
                                               'mac_address': '00155D000001',
                                               'mac_address_colons': '00:15:5d:00:00:01',
                                               'switch_name': 'External'}]
        _run(FakeTask())
        assert f'tag={hyperv_xhm.DEFAULT_IMPORT_VLAN}' in self._created(target)['net0']

    def test_the_operator_choice_beats_both(self, db, wired):
        """The wizard's field is the last word: it is what the person actually saw."""
        source, target, _ = wired
        source._detail['network_adapters'] = [{'name': 'Network Adapter',
                                               'mac_address': '00155D000001',
                                               'mac_address_colons': '00:15:5d:00:00:01',
                                               'switch_name': 'External',
                                               'vlan_mode': 'Access', 'vlan_id': 22}]
        _run(FakeTask(config={'vlan_map': {'00155D000001': 99}}))
        assert 'tag=99' in self._created(target)['net0']
        assert 'tag=22' not in self._created(target)['net0']

    def test_an_emptied_vlan_field_means_no_tag_rather_than_the_default(self, db, wired):
        """Clearing the box is a decision, so it must not fall back to the default.

        Only an adapter the operator never touched falls through to the source's value.
        """
        source, target, _ = wired
        source._detail['network_adapters'] = [{'name': 'Network Adapter',
                                               'mac_address': '00155D000001',
                                               'mac_address_colons': '00:15:5d:00:00:01',
                                               'switch_name': 'External',
                                               'vlan_mode': 'Access', 'vlan_id': 22}]
        _run(FakeTask(config={'vlan_map': {'00155D000001': ''}}))
        assert 'tag=' not in self._created(target)['net0']

    def test_a_trunk_adapter_arrives_untagged_rather_than_on_a_guessed_vlan(self, db, wired):
        """A trunk carries several ids, so there is no single one to carry over.

        Putting it on one guessed VLAN would look like it worked, which is the failure
        this avoids; the preflight names the adapter instead.
        """
        source, target, _ = wired
        source._detail['network_adapters'] = [{'name': 'Network Adapter',
                                               'mac_address': '00155D000001',
                                               'mac_address_colons': '00:15:5d:00:00:01',
                                               'switch_name': 'External',
                                               'vlan_mode': 'Trunk', 'vlan_id': 0}]
        _run(FakeTask())
        assert 'tag=' not in self._created(target)['net0']

    def test_every_adapter_gets_its_own_bridge_and_vlan(self, db, wired):
        """Multi-NIC is the common case in a real estate, not the exception."""
        source, target, _ = wired
        source._detail['network_adapters'] = [
            {'name': 'LAN', 'mac_address': '00155D000001',
             'mac_address_colons': '00:15:5d:00:00:01', 'switch_name': 'External',
             'vlan_mode': 'Access', 'vlan_id': 22},
            {'name': 'DMZ', 'mac_address': '00155D000002',
             'mac_address_colons': '00:15:5d:00:00:02', 'switch_name': 'DMZ',
             'vlan_mode': 'Access', 'vlan_id': 33},
        ]
        _run(FakeTask(), network_map={'00155D000001': 'vmbr0', '00155D000002': 'vmbr1'})
        created = self._created(target)
        assert 'bridge=vmbr0' in created['net0'] and 'tag=22' in created['net0']
        assert 'bridge=vmbr1' in created['net1'] and 'tag=33' in created['net1']
        assert '00:15:5d:00:00:02' in created['net1']

    def test_an_adapter_that_has_never_had_a_mac_gets_one_from_the_target(self, db, wired):
        """A VM that has never started reports all zeroes, which is not an address.

        Carrying it over is refused by the target. Leaving it out lets Proxmox assign one,
        which is what the source would have done at its own first start.
        """
        source, target, _ = wired
        source._detail['network_adapters'] = [{'name': 'Network Adapter',
                                              'mac_address': '000000000000',
                                              'mac_address_colons': '00:00:00:00:00:00',
                                              'switch_name': 'External'}]
        # Keyed by position, not by the zeroes: all-zero is the absence of an address, so
        # it cannot address the adapter either. Two such adapters would share one key.
        _run(FakeTask(), network_map={'adapter1': 'vmbr0'})
        net0 = self._created(target)['net0']
        assert 'macaddr' not in net0
        assert 'bridge=vmbr0' in net0

    def test_the_vm_is_not_configured_before_its_creation_task_finishes(self, db, wired):
        """Creating a VM holds a lock, and posting a disk into it is refused.

        Measured on Proxmox VE 9.2.11: without the wait, every single attach failed with
        "VM is locked (create)" and the VM was left with no disks at all.
        """
        _, target, _ = wired
        _run(FakeTask())
        assert target.waited_for is not None, 'the creation task was never waited for'
        node, upid = target.waited_for
        assert upid.startswith('UPID:')

    def test_a_disk_that_cannot_be_attached_fails_the_migration(self, db, wired):
        """A VM whose disks are missing boots to a network prompt.

        Reporting that as completed sends somebody to a machine they believe is migrated,
        so the run fails — while the converted volumes stay where they are, recorded, for
        an operator to attach or clean up.
        """
        _, target, _ = wired
        target.refuse_posts = ('sata0', 'scsi0')
        task = _run(FakeTask())
        assert task.phase == 'failed'
        assert 'could not be attached' in (task.error or '')

    def test_the_data_is_kept_when_attaching_fails(self, db, wired):
        """Failing the run must not throw away a conversion that just succeeded."""
        _, target, node = wired
        target.refuse_posts = ('sata0', 'scsi0')
        _run(FakeTask())
        assert not any('pvesm free' in command for command in node.commands), (
            'the converted volume was freed even though the data was good')

    def test_the_default_hardware_needs_no_drivers_the_guest_does_not_have(self, db, wired):
        """An imported Windows guest has whatever drivers it had on Hyper-V.

        That does not include VirtIO, so the default target is SATA and e1000: emulations
        every supported guest already has a driver for.
        """
        _, target, _ = wired
        _run(FakeTask())
        created = self._created(target)
        assert created['net0'].startswith('e1000,')
        assert any('sata0' in data for _, data in target.posts)

    def test_choosing_virtio_moves_the_disk_and_the_card_together(self, db, wired):
        """The warning and the created hardware are one decision, not two.

        Choosing SATA used to silence the VirtIO driver warning and then attach the disk to
        VirtIO SCSI anyway, and the network card stayed on VirtIO regardless.
        """
        _, target, _ = wired
        task = FakeTask()
        task.config = {'hardware': 'virtio'}
        _run(task)
        created = self._created(target)
        assert created['net0'].startswith('virtio,')
        assert any('scsi0' in data for _, data in target.posts)

    def test_choosing_compatible_moves_both_back(self, db, wired):
        _, target, _ = wired
        task = FakeTask()
        task.config = {'hardware': 'compatible'}
        _run(task)
        created = self._created(target)
        assert created['net0'].startswith('e1000,')
        assert any('sata0' in data for _, data in target.posts)

    def test_the_guest_operating_system_is_not_guessed(self, db, wired):
        """Nothing on this side can see inside the guest."""
        _, target, _ = wired
        _run(FakeTask())
        assert self._created(target)['ostype'] == 'other'

    def test_the_migrated_vm_is_not_started(self, db, wired):
        """It boots when somebody has looked at it. An automatic start would put a second
        copy of a live machine on the network beside the original."""
        _, target, _ = wired
        _run(FakeTask())
        assert not any('/status/start' in url for url, _ in target.posts)


# ===========================================================================
# The durable record
# ===========================================================================

class TestWhatSurvivesTheProcess:
    def test_everything_created_on_the_target_is_recorded(self, db, wired):
        """This is the list that makes an interrupted migration recoverable by a person:
        what exists now that did not exist before."""
        task = _run(FakeTask())
        row = hyperv_db.get_migration(db.conn, task.id)
        kinds = {resource['kind'] for resource in row['created_resources']}
        assert {'volume', 'vm'} <= kinds

    def test_a_failed_run_is_recorded_as_failed_with_its_reason(self, db, wired):
        _, _, node = wired
        node.convert_exit = 1
        task = _run(FakeTask())

        row = hyperv_db.get_migration(db.conn, task.id)
        assert row['status'] == hyperv_db.STATUS_FAILED
        assert row['error']

    def test_a_completed_run_is_recorded_as_completed(self, db, wired):
        task = _run(FakeTask())
        row = hyperv_db.get_migration(db.conn, task.id)
        assert row['status'] == hyperv_db.STATUS_COMPLETED
        assert row['target_vmid'] == 120


# ===========================================================================
# One import at a time, and what a failed one leaves behind
# ===========================================================================

class TestOneImportAtATime:
    """Two starts on one source must not become two transfers of the same disks.

    The danger is not a confusing UI. Each run allocates its own volumes and its own
    target VMID, so a second one copies the same hundreds of gigabytes into a second
    place, fills the storage, and leaves two half-machines that nobody can tell apart.
    """

    def test_a_second_start_while_one_runs_creates_nothing(self, db, wired):
        source, target, node = wired
        # What the first runner has done by the time the second one starts: recorded the
        # migration, then claimed the source. A claim is live only while its migration is.
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   migration_id='mig-first')
        assert hyperv_db.claim_source(db.conn, SOURCE, GUID, 'mig-first') is None

        second = _run(FakeTask(id='mig-second'))

        assert second.status == 'failed'
        assert 'mig-first' in second.error
        assert target.posts == [], 'the loser created something on the target'
        # The refused attempt is recorded rather than silently dropped, and it recorded
        # nothing as created, so it never blocks a later start.
        row = hyperv_db.get_migration(db.conn, 'mig-second')
        assert row['status'] == hyperv_db.STATUS_FAILED
        assert row['created_resources'] == []
        # And the loser's cleanup must not release the winner's claim on its way out.
        held = hyperv_db.active_claim(db.conn, SOURCE, GUID)
        assert held and held['migration_id'] == 'mig-first'

    def test_the_claim_is_given_back_when_the_run_ends(self, db, wired):
        task = _run(FakeTask(id='mig-done'))
        assert task.status == 'completed'
        assert hyperv_db.active_claim(db.conn, SOURCE, GUID) is None

    def test_the_claim_is_given_back_when_the_run_fails(self, db, wired, monkeypatch):
        monkeypatch.setattr(hyperv_xhm, '_open_target_node',
                            lambda *a: (_ for _ in ()).throw(TransferError('no route')))
        task = _run(FakeTask(id='mig-broken'))
        assert task.status == 'failed'
        assert hyperv_db.active_claim(db.conn, SOURCE, GUID) is None

    def test_a_claim_whose_migration_ended_does_not_block_forever(self, db, wired):
        """A process that dies between the last write and the release leaves a claim.

        Liveness is decided by the migration's status, not by the claim row, so a restart
        cannot leave a VM permanently unmigratable.
        """
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   migration_id='mig-dead')
        hyperv_db.claim_source(db.conn, SOURCE, GUID, 'mig-dead')
        hyperv_db.update_migration(db.conn, 'mig-dead',
                                   status=hyperv_db.STATUS_INTERRUPTED)

        assert hyperv_db.active_claim(db.conn, SOURCE, GUID) is None
        assert hyperv_xhm.refuse_hyperv_start(SOURCE, VMID) is None


class TestStartingAgainAfterAFailure:
    def test_a_clean_host_is_not_refused(self, db, wired):
        assert hyperv_xhm.refuse_hyperv_start(SOURCE, VMID) is None

    def test_a_live_import_refuses_the_next_one(self, db, wired):
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   migration_id='mig-live')
        hyperv_db.claim_source(db.conn, SOURCE, GUID, 'mig-live')

        refused = hyperv_xhm.refuse_hyperv_start(SOURCE, VMID)
        assert refused and 'mig-live' in refused

    def test_leftovers_from_a_failed_import_refuse_the_next_one(self, db, wired):
        """Otherwise the same disks are copied a second time into a second set of volumes.

        That is the failure the ticket calls "silently duplicated": nothing errors, the
        storage simply fills with copies whose origin nobody can reconstruct afterwards.
        """
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   migration_id='mig-old')
        hyperv_db.record_created_resource(db.conn, 'mig-old', 'volume',
                                          'local-lvm:vm-120-disk-0')
        hyperv_db.update_migration(db.conn, 'mig-old', status=hyperv_db.STATUS_FAILED)

        refused = hyperv_xhm.refuse_hyperv_start(SOURCE, VMID)
        assert refused and 'mig-old' in refused and 'vm-120-disk-0' in refused

    def test_a_failed_import_that_left_nothing_does_not_refuse(self, db, wired):
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   migration_id='mig-clean')
        hyperv_db.update_migration(db.conn, 'mig-clean', status=hyperv_db.STATUS_FAILED)

        assert hyperv_xhm.refuse_hyperv_start(SOURCE, VMID) is None

    def test_a_freed_volume_stops_being_listed_as_a_leftover(self, db, wired, monkeypatch):
        """A failed conversion frees its volume and starts over on a fresh one.

        If the record kept naming the freed volume, the next start would be refused
        because of something that is not there, and a cleanup would go looking for it.
        """
        source, target, _ = wired
        failing = FakeNode(convert_exit=1)
        monkeypatch.setattr(hyperv_xhm, '_open_target_node',
                            lambda *a: (failing, '/mnt/x.credentials'))

        task = _run(FakeTask(id='mig-retry'))
        assert task.status == 'failed'

        left = hyperv_db.get_migration(db.conn, 'mig-retry')['created_resources']
        assert left == [], f'freed volumes are still recorded: {left}'
        assert hyperv_xhm.refuse_hyperv_start(SOURCE, VMID) is None


class TestCleaningUpWhatAFailedImportLeft:
    """Removing target resources is the one destructive thing this patch can do."""

    def _failed_migration(self, db, target, migration_id='mig-left'):
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   target_cluster=TARGET, target_node='node-a',
                                   migration_id=migration_id)
        hyperv_db.update_migration(db.conn, migration_id, target_vmid=120,
                                   status=hyperv_db.STATUS_FAILED)
        hyperv_db.record_created_resource(db.conn, migration_id, 'vm', '120')
        hyperv_db.record_created_resource(db.conn, migration_id, 'volume',
                                          'local-lvm:vm-120-disk-0')
        target.vm_configs = {'120': {'description':
                                     hyperv_xhm.target_vm_description(migration_id, 'guest-a')}}
        return migration_id

    @pytest.fixture
    def ssh(self, monkeypatch):
        """The node a cleanup frees volumes on, recording what it was asked to run."""
        node = FakeNode()

        class _Ssh:
            def close(self):
                node.closed = True

        import pegaprox.core.xhm as core_xhm
        monkeypatch.setattr(core_xhm, '_resolve_pve_node_ip', lambda t, n: '127.0.0.1')
        monkeypatch.setattr(core_xhm, '_connect_ssh', lambda *a, **kw: _Ssh())
        monkeypatch.setattr(hyperv_xhm, '_Node', lambda ssh, user='root': node)
        return node

    def test_without_confirmation_nothing_is_touched(self, db, wired, ssh):
        _, target, _ = wired
        mid = self._failed_migration(db, target)

        result = hyperv_xhm.cleanup_migration(mid, confirmed=False)

        assert result['success'] is False
        assert target.deleted == []
        assert hyperv_db.get_migration(db.conn, mid)['created_resources']

    def test_a_running_migration_is_not_cleaned_up_under_its_own_worker(self, db, wired, ssh):
        _, target, _ = wired
        mid = self._failed_migration(db, target, 'mig-running')
        hyperv_db.update_migration(db.conn, mid, status=hyperv_db.STATUS_RUNNING)

        result = hyperv_xhm.cleanup_migration(mid, confirmed=True)

        assert result['success'] is False
        assert 'still running' in result['error']
        assert target.deleted == []

    def test_a_second_import_on_the_same_source_blocks_the_cleanup(self, db, wired, ssh):
        """Deleting target resources while another run writes to that source is how two
        runs corrupt each other."""
        _, target, _ = wired
        mid = self._failed_migration(db, target)
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   migration_id='mig-other')
        hyperv_db.claim_source(db.conn, SOURCE, GUID, 'mig-other')

        result = hyperv_xhm.cleanup_migration(mid, confirmed=True)

        assert result['success'] is False
        assert 'mig-other' in result['error']
        assert target.deleted == []

    def test_a_confirmed_cleanup_removes_the_vm_and_frees_the_volume(self, db, wired, ssh):
        _, target, _ = wired
        mid = self._failed_migration(db, target)

        result = hyperv_xhm.cleanup_migration(mid, confirmed=True)

        assert result['success'] is True, result
        assert any(url.endswith('/qemu/120') for url in target.deleted)
        assert any(c.startswith('pvesm free') and 'vm-120-disk-0' in c
                   for c in ssh.commands)
        assert hyperv_db.get_migration(db.conn, mid)['created_resources'] == []

    def test_a_vmid_that_now_belongs_to_somebody_else_is_left_alone(self, db, wired, ssh):
        """A VMID is not ownership. Between a failed import and a cleanup the number can
        have been handed to a guest that has nothing to do with this migration, and its
        disks are that guest's disks."""
        _, target, _ = wired
        mid = self._failed_migration(db, target)
        target.vm_configs = {'120': {'description': 'Production database. Do not delete.'}}

        result = hyperv_xhm.cleanup_migration(mid, confirmed=True)

        assert result['success'] is False
        assert target.deleted == []
        assert not [c for c in ssh.commands if c.startswith('pvesm free')]
        assert hyperv_db.get_migration(db.conn, mid)['created_resources']

    def test_a_target_vm_that_is_already_gone_counts_as_removed(self, db, wired, ssh):
        _, target, _ = wired
        mid = self._failed_migration(db, target)
        target.vm_configs = {}

        result = hyperv_xhm.cleanup_migration(mid, confirmed=True)

        assert result['success'] is True, result
        assert target.deleted == []

    def test_the_source_is_never_part_of_a_cleanup(self, db, wired, ssh):
        """The rollback for this direction is "start the original again", which only
        works while the original is still there."""
        source, target, _ = wired
        mid = self._failed_migration(db, target)
        source.calls = []
        source.stop_vm = lambda *a, **kw: source.calls.append('stop')
        source.delete_vm = lambda *a, **kw: source.calls.append('delete')

        hyperv_xhm.cleanup_migration(mid, confirmed=True)

        assert source.calls == []


# ===========================================================================
# Cutover: one machine, running once
# ===========================================================================

class TestNeitherSideIsStartedWhileTheOtherRuns:
    """The copy carries the original's hostname and MAC, and nothing merges divergence.

    So PegaProx refuses to start either one while it can see the other running — and
    refuses just as firmly when it cannot see the other at all, because "I could not read
    it" is not "it is off".
    """

    def _completed(self, db, migration_id='mig-cut'):
        hyperv_db.create_migration(db.conn, source_cluster=SOURCE, source_vm_guid=GUID,
                                   source_vm_name='guest-a', target_cluster=TARGET,
                                   target_node='node-a', migration_id=migration_id)
        hyperv_db.update_migration(db.conn, migration_id, target_vmid=120,
                                   status=hyperv_db.STATUS_COMPLETED)
        return migration_id

    def _target_with(self, target, mid, status, description=None):
        if description is None:
            description = hyperv_xhm.target_vm_description(mid, 'guest-a')
        target.vm_configs = {'120': {'description': description}}
        target.vm_status = {'120': {'status': status}}

    @pytest.fixture(autouse=True)
    def _status_route(self, monkeypatch):
        """Teach the fake target to answer /status/current, which only this pair reads."""
        def _api_get(self, url):
            if url.endswith('/status/current'):
                vmid = url.rsplit('/qemu/', 1)[-1].split('/')[0]
                payload = getattr(self, 'vm_status', {}).get(vmid)
                if payload is None:
                    return FakeResponse(status_code=404)
                return FakeResponse(payload={'data': payload})
            return FakeTarget._api_get_original(self, url)

        if not hasattr(FakeTarget, '_api_get_original'):
            FakeTarget._api_get_original = FakeTarget._api_get
        monkeypatch.setattr(FakeTarget, '_api_get', _api_get)

    def test_the_source_is_not_started_while_the_copy_runs(self, db, wired):
        _, target, _ = wired
        mid = self._completed(db)
        self._target_with(target, mid, 'running')

        refused = hyperv_xhm.refuse_source_start(SOURCE, VMID)
        assert refused and '120' in refused

    def test_the_source_starts_when_the_copy_is_stopped(self, db, wired):
        _, target, _ = wired
        mid = self._completed(db)
        self._target_with(target, mid, 'stopped')

        assert hyperv_xhm.refuse_source_start(SOURCE, VMID) is None

    def test_an_unreadable_copy_blocks_the_source_start(self, db, wired, monkeypatch):
        """The acceptance criterion names this case on its own: an unknown counter-state
        blocks, because acting on a guess is how both end up running."""
        _, target, _ = wired
        mid = self._completed(db)
        self._target_with(target, mid, 'running')
        monkeypatch.setattr(FakeTarget, '_api_get',
                            lambda self, url: (_ for _ in ()).throw(OSError('no route')))

        refused = hyperv_xhm.refuse_source_start(SOURCE, VMID)
        assert refused and 'cannot be read' in refused

    def test_a_copy_that_no_longer_exists_does_not_block(self, db, wired):
        _, target, _ = wired
        self._completed(db)
        target.vm_configs = {}
        target.vm_status = {}

        assert hyperv_xhm.refuse_source_start(SOURCE, VMID) is None

    def test_the_copy_is_not_started_while_the_original_runs(self, db, wired):
        source, target, _ = wired
        mid = self._completed(db)
        self._target_with(target, mid, 'stopped')
        source.get_vm = lambda guid: {'state': 'Running'}

        refused = hyperv_xhm.refuse_target_start(TARGET, 120)
        assert refused and 'guest-a' in refused

    def test_the_copy_starts_when_the_original_is_off(self, db, wired):
        source, target, _ = wired
        mid = self._completed(db)
        self._target_with(target, mid, 'stopped')
        source.get_vm = lambda guid: {'state': 'Off'}

        assert hyperv_xhm.refuse_target_start(TARGET, 120) is None

    def test_a_vmid_that_is_not_this_copy_is_never_blocked(self, db, wired):
        """A VMID outlives the migration that used it. Blocking an unrelated guest's start
        forever because of a number would be worse than the collision it guards against."""
        source, target, _ = wired
        mid = self._completed(db)
        self._target_with(target, mid, 'stopped', description='Someone else\'s VM')
        source.get_vm = lambda guid: {'state': 'Running'}

        assert hyperv_xhm.refuse_target_start(TARGET, 120) is None

    def test_an_unreadable_original_blocks_the_copy_start(self, db, wired):
        from pegaprox.core.hyperv_errors import HyperVError, KIND_UNREACHABLE

        source, target, _ = wired
        mid = self._completed(db)
        self._target_with(target, mid, 'stopped')

        def _raise(guid):
            raise HyperVError(KIND_UNREACHABLE, 'no connection')
        source.get_vm = _raise

        refused = hyperv_xhm.refuse_target_start(TARGET, 120)
        assert refused and 'cannot be read' in refused


class TestOptionsThisDirectionRefuses:
    """One is still refused; the other became a choice, and the difference is the point."""

    def test_remove_source_cannot_be_switched_on_by_a_direct_request(self, db, wired):
        """Deleting the source removes the rollback, and no confirmation restores it."""
        refused = hyperv_xhm.refuse_hyperv_start(SOURCE, VMID, {'remove_source': True})
        assert refused and 'never deletes' in refused

    def test_starting_the_copy_is_allowed_and_is_the_operator_s_call(self, db, wired):
        """It used to be refused outright, which is why no migration could be started.

        The wizard hid the checkbox and kept its default of true, so every request carried
        an option the operator had never seen and could not clear. Refusing a real risk is
        not the same as deciding it for somebody: a maintenance window where the source has
        just been shut down for good is exactly when starting the copy is what is wanted.
        """
        assert hyperv_xhm.refuse_hyperv_start(SOURCE, VMID, {'start_after': True}) is None

    def test_the_ordinary_request_is_not_refused(self, db, wired):
        assert hyperv_xhm.refuse_hyperv_start(
            SOURCE, VMID, {'start_after': False, 'remove_source': False}) is None

    def test_the_runner_never_deletes_the_source(self, db, wired):
        """Belt and braces: even if a request got past the route, nothing acts on it."""
        import inspect
        source = inspect.getsource(hyperv_xhm._run_hyperv_to_pve)
        assert 'remove_source' not in source


class TestStartingTheImportedVm:
    """Off unless asked for, and asked for means asked for."""

    def test_nothing_is_started_when_nobody_asked(self, db, wired):
        _, target, _ = wired
        _run(FakeTask())
        assert not [url for url, _ in target.posts if url.endswith('/status/start')]

    def test_the_vm_is_started_when_the_request_says_so(self, db, wired):
        _, target, _ = wired
        _run(FakeTask(config={'start_after': True}))
        started = [url for url, _ in target.posts if url.endswith('/status/start')]
        assert started, 'the migration was asked to start the VM and did not'

    def test_a_start_that_fails_does_not_fail_the_migration(self, db, wired, monkeypatch):
        """The VM exists and is correct; starting it is one click on the target."""
        _, target, _ = wired
        monkeypatch.setattr(hyperv_xhm, '_start_target',
                            lambda task, vmid: task.log('boom'))
        task = _run(FakeTask(config={'start_after': True}))
        assert task.phase == 'completed'


# ===========================================================================
# What a large import costs
# ===========================================================================

class TestTheProgressReaderIsBounded:
    """A transfer's size must change its duration and nothing else.

    The data never passes through PegaProx — the target node converts the disk itself and
    the only thing read here is a progress percentage. That claim is only true while this
    reader keeps a bounded amount of what it reads, so it is asserted rather than assumed:
    a reader that appended would hold the whole of a multi-hour conversion's output, and
    the symptom would be a management server that grows with the disk it is copying.
    """

    class _Channel:
        """A paramiko channel that produces a great deal of progress and then exits."""

        def __init__(self, chunks):
            self._remaining = chunks
            self.closed = False

        def exit_status_ready(self):
            return self._remaining <= 0

        def recv_ready(self):
            return self._remaining > 0

        def recv(self, size):
            self._remaining -= 1
            percent = 100.0 * (1 - self._remaining / 200_000)
            return f'    ({percent:.2f}/100%)\r'.encode()

        def recv_exit_status(self):
            return 0

        def close(self):
            self.closed = True

    class _Ssh:
        def __init__(self, channel):
            self._channel = channel

        def exec_command(self, command, timeout=None):
            class _Std:
                def __init__(self, channel):
                    self.channel = channel

                def read(self):
                    return b''
            return None, _Std(self._channel), _Std(self._channel)

    def test_two_hundred_thousand_progress_reads_keep_one_chunk(self):
        channel = self._Channel(200_000)
        node = hyperv_xhm._Node(self._Ssh(channel))
        seen = []

        exit_code, stdout, _ = node.run_with_progress(
            'qemu-img convert ...', lambda percent: seen.append(percent), lambda: False)

        assert exit_code == 0
        assert len(seen) == 200_000, 'progress was dropped rather than reported'
        # The whole output would be megabytes; what is kept is the last read.
        assert len(stdout) < 4096, f'the reader accumulated {len(stdout)} characters'

    def test_a_hundred_times_the_progress_costs_no_more_memory(self):
        """The figure docs/hyperv-transfer.md publishes, asserted rather than observed once.

        Character counts bound what the reader keeps; they say nothing about what it
        allocates on the way. A reader that built a new list per read would satisfy the
        test above and still grow with the length of a conversion, so the allocation
        itself is measured — at two volumes, because the invariant is that the second
        number is not larger than the first.
        """
        import tracemalloc

        def peak_for(reads):
            channel = self._Channel(reads)
            node = hyperv_xhm._Node(self._Ssh(channel))
            tracemalloc.start()
            try:
                node.run_with_progress(
                    'qemu-img convert ...', lambda percent: None, lambda: False)
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

        small = peak_for(2_000)
        large = peak_for(200_000)

        assert large <= small * 2, (
            f'the reader peaked at {large} bytes for a hundred times the progress of the '
            f'{small} bytes it needed for the small run')

    def test_a_cancelled_conversion_stops_reading_and_closes_the_channel(self):
        channel = self._Channel(200_000)
        node = hyperv_xhm._Node(self._Ssh(channel))

        exit_code, _, error = node.run_with_progress(
            'qemu-img convert ...', lambda percent: None, lambda: True)

        assert exit_code == -1
        assert error == 'cancelled'
        assert channel.closed, 'a cancelled conversion left its channel open'


def _kinds(commands):
    """Label each command the runner issued, so a count failure says what changed.

    Matching on a prefix is not enough: the share is mounted with a command that begins
    `mkdir -p … && mount -t cifs …`, so a test that looked for one starting with "mount "
    would count none and compare zero against zero.
    """
    labels = []
    for command in commands:
        if 'mount -t cifs' in command:
            labels.append('mount')
        elif command.startswith('umount '):
            labels.append('umount')
        elif 'qemu-img convert' in command:
            labels.append('convert')
        elif command.startswith('pvesm alloc'):
            labels.append('allocate')
        elif command.startswith('pvesm path'):
            labels.append('resolve')
        elif command.startswith('test -r'):
            labels.append('probe')
        elif command.startswith('rm -f'):
            labels.append('remove-credentials')
        else:
            labels.append(f'other: {command[:40]}')
    return labels


class TestTheTransferTakesTheAddressItWasGiven:
    """Which address the target node mounts the share from.

    Management reaches a Hyper-V host over whatever interface its admin address is on,
    and on this estate that is 1 GbE while a separate 10 GbE segment carries backups and
    migrations. The transfer has to be able to take the second one. Naming the address is
    how that happens — the node picks the interface from its route to it — so there is no
    interface setting anywhere, and an interface name on the PegaProx side would say
    nothing about which way the node routes.
    """

    def test_without_one_the_management_address_is_used(self, db, wired):
        source, target, node = wired
        _run(FakeTask())
        mounts = [c for c in node.commands if 'mount -t cifs' in c]
        assert mounts, 'nothing was mounted'
        assert f'//{source.config.host}/' in mounts[0]

    def test_with_one_the_transfer_address_is_used(self, db, wired):
        source, target, node = wired
        source.config.transfer_host = 'source-host-fast.invalid'
        _run(FakeTask())
        mounts = [c for c in node.commands if 'mount -t cifs' in c]
        assert mounts, 'nothing was mounted'
        assert '//source-host-fast.invalid/' in mounts[0]
        # And not the management one — otherwise the setting reads as applied while the
        # copy still crawls down the admin interface.
        assert f'//{source.config.host}/' not in mounts[0]

    def test_a_blank_setting_is_not_a_host_called_nothing(self, db, wired):
        source, target, node = wired
        source.config.transfer_host = '   '
        _run(FakeTask())
        mounts = [c for c in node.commands if 'mount -t cifs' in c]
        assert f'//{source.config.host}/' in mounts[0]


class TestWhatOneImportCostsTheHosts:
    """How much a transfer asks of either end, counted rather than estimated."""

    def test_one_disk_costs_these_seven_calls_and_no_others(self, db, wired):
        """The figures docs/hyperv-transfer.md publishes, locked where they can drift.

        The test below proves the count does not grow with the disk; this one proves what
        the count actually is. Without it the document could keep naming seven long after
        the runner had started issuing nine, and nothing would fail.
        """
        source, target, node = wired
        _run(FakeTask(id='mig-seven'))

        assert _kinds(node.commands) == [
            'mount', 'probe', 'allocate', 'resolve', 'convert',
            'umount', 'remove-credentials']

    def test_two_disks_on_one_drive_cost_eleven_calls_and_one_mount(self, db, wired):
        source, target, node = wired
        source._detail['disks'] = [
            dict(source._detail['disks'][0]),
            {**source._detail['disks'][0], 'path': 'C:\\vm\\b.vhdx'},
        ]
        _run(FakeTask(id='mig-eleven'))

        kinds = _kinds(node.commands)
        assert len(kinds) == 11, f'{len(kinds)} calls for two disks: {kinds}'
        assert kinds.count('mount') == 1, 'a second disk on the same drive remounted'
        assert kinds.count('convert') == 2

    def test_the_number_of_remote_calls_does_not_depend_on_the_disk(self, db, wired):
        """A constant per disk, not one per gigabyte.

        The alternative shape — read a block, write a block, ask again — is what makes a
        remote import take longer than the copy itself. Here the node is told once what to
        convert and then only watched, so the call count is a property of the VM rather
        than of its size.
        """
        source, target, node = wired
        source._detail['disks'][0]['size'] = 42949672960          # 40 GiB
        _run(FakeTask(id='mig-small'))
        small = len(node.commands)

        node.commands.clear()
        source._detail['disks'][0]['size'] = 42949672960 * 100    # 4 TiB
        _run(FakeTask(id='mig-large'))

        assert len(node.commands) == small, (
            f'{len(node.commands)} calls for a disk a hundred times the size, against '
            f'{small} for the small one')

    def test_a_second_disk_costs_a_second_conversion_and_nothing_else(self, db, wired):
        """The share is mounted per share, not per disk: two disks on one drive is one
        mount, and the difference between one disk and two is the conversion itself."""
        source, target, node = wired
        _run(FakeTask(id='mig-one'))
        one = len(node.commands)
        mounts_for_one = _kinds(node.commands).count('mount')

        node.commands.clear()
        source._detail['disks'] = [
            dict(source._detail['disks'][0]),
            {**source._detail['disks'][0], 'path': 'C:\\vm\\b.vhdx'},
        ]
        _run(FakeTask(id='mig-two'))

        mounts_for_two = _kinds(node.commands).count('mount')
        assert mounts_for_one == 1, 'the one-disk run did not mount the share at all'
        assert mounts_for_two == mounts_for_one, 'a second disk on the same drive remounted'
        assert len(node.commands) > one


class TestTwoSourcesSideBySide:
    """Several Hyper-V hosts, each connected on its own, must not share a numbering.

    Two hosts whose VMs collided on a synthetic VMID would merge their access-control
    entries — the quietest possible way to hand somebody another customer's VM.
    """

    def test_each_host_numbers_its_own_vms(self, db):
        from pegaprox.core import hyperv_db as store

        first = store.get_vmid(db.conn, 'hv_1', GUID, 'guest-a')
        second = store.get_vmid(db.conn, 'hv_2', GUID, 'guest-a')

        assert first == store.get_vmid(db.conn, 'hv_1', GUID, 'guest-a')
        assert store.resolve_vmid(db.conn, 'hv_1', first) == GUID
        assert store.resolve_vmid(db.conn, 'hv_2', first) in (None, GUID)
        # The same GUID on two hosts is two VMs as far as this product is concerned, and
        # each host's number means something only on that host.
        assert store.resolve_vmid(db.conn, 'hv_2', second) == GUID

    def test_a_migration_of_one_host_does_not_claim_the_other(self, db):
        from pegaprox.core import hyperv_db as store

        store.create_migration(db.conn, source_cluster='hv_1', source_vm_guid=GUID,
                               migration_id='mig-host-one')
        assert store.claim_source(db.conn, 'hv_1', GUID, 'mig-host-one') is None

        store.create_migration(db.conn, source_cluster='hv_2', source_vm_guid=GUID,
                               migration_id='mig-host-two')
        assert store.claim_source(db.conn, 'hv_2', GUID, 'mig-host-two') is None


class TestRunningOnANodeWhoseAccountIsNotRoot:
    """PegaProx supports a non-root node account, and this transfer needs root anyway.

    It mounts an SMB share, allocates a volume and runs qemu-img. Without the wrapper a
    correctly configured cluster failed with a bare `Permission denied` from mount, several
    steps after the target VM had already been created.
    """

    class _Recorder:
        def __init__(self):
            self.command = ''
            self.stdin_was_readable = None

        def exec_command(self, command, timeout=None):
            self.command = command

            class _Stdin:
                def __init__(self, outer):
                    self._outer = outer
                    self.channel = self

                def write(self, data):
                    self._outer.stdin_was_readable = data

                def shutdown_write(self):
                    pass

            class _Stream:
                def __init__(self):
                    self.channel = self

                def read(self):
                    return b''

                def recv_exit_status(self):
                    return 0

            return _Stdin(self), _Stream(), _Stream()

    def test_a_root_account_runs_the_command_unchanged(self):
        recorder = self._Recorder()
        hyperv_xhm._Node(recorder, 'root').run('pvesm alloc x')
        assert recorder.command == 'pvesm alloc x'

    def test_another_account_runs_it_through_sudo(self):
        recorder = self._Recorder()
        hyperv_xhm._Node(recorder, 'pegaprox').run('pvesm alloc x')
        assert recorder.command.startswith('sudo -n bash -c ')
        assert 'pvesm alloc x' in recorder.command

    def test_the_wrapper_leaves_stdin_free_for_the_credentials(self):
        """The share credentials are written with `cat` and must not travel in argv.

        A wrapper that pipes the script in on stdin — which is what PegaProx' own
        `_wrap_with_sudo` does — takes that stdin for itself and writes an empty file.
        """
        recorder = self._Recorder()
        hyperv_xhm._Node(recorder, 'pegaprox').run('umask 077 && cat > /tmp/c',
                                                   stdin_data='username=probe\n')
        assert recorder.stdin_was_readable == 'username=probe\n'
        assert 'username=probe' not in recorder.command


class TestAConfiguredSshKeyIsActuallyUsed:
    """`config.ssh_key` holds the key material, not a path to it.

    The cluster form is a textarea and the row is stored encrypted; there is no file. Every
    caller in `core/xhm.py` passes that value as `key_path`, and the helper tested it with
    `os.path.exists()` — False for a PEM block — so key authentication was skipped in
    silence and the connection fell through to the password. On a node that accepts
    publickey only, which is the Proxmox default after hardening, every cross-hypervisor
    migration failed at its first node command on a correctly configured cluster.
    """

    # Assembled rather than pasted: a literal PEM block in a source file is what secret
    # scanners are built to find, and this one is a fixture, not a key.
    _EDGE = '-' * 5
    _LABEL = 'OPENSSH ' + 'PRIVATE ' + 'KEY'
    KEY = (f'{_EDGE}BEGIN {_LABEL}{_EDGE}\n'
           'bm90LWEtcmVhbC1rZXktanVzdC1hLWZpeHR1cmU=\n'
           f'{_EDGE}END {_LABEL}{_EDGE}')

    def test_key_material_becomes_a_file_paramiko_can_read(self):
        from pegaprox.core import xhm as core_xhm
        path, temporary = core_xhm._key_file_for(self.KEY)
        try:
            assert temporary is True
            with open(path, encoding='utf-8') as handle:
                written = handle.read()
            assert self._LABEL in written
            # OpenSSH refuses a key file that does not end in a newline.
            assert written.endswith('\n')
        finally:
            import os
            os.unlink(path)

    def test_an_existing_path_is_passed_through_untouched(self, tmp_path):
        from pegaprox.core import xhm as core_xhm
        key_file = tmp_path / 'id_test'
        key_file.write_text(self.KEY + '\n')
        path, temporary = core_xhm._key_file_for(str(key_file))
        assert path == str(key_file)
        assert temporary is False

    def test_a_cluster_without_a_key_stays_on_the_password_path(self):
        from pegaprox.core import xhm as core_xhm
        assert core_xhm._key_file_for('') == (None, False)
        assert core_xhm._key_file_for('not a key at all') == (None, False)


class TestAnImportThatInstallsNoDrivers:
    """The compatible path still has to make the disk bootable on a new platform.

    A guest that shut down with Fast Startup left a saved kernel session behind. Resuming
    it against a different chipset and timer is not supported by Windows, and the
    compatible controller does not change that — it decides whether the loader can read
    the disk, not what the resumed kernel finds attached to it. What the bytes survive is
    measured in tests/hyperv_testbed/verify_hibernation_clear.sh.
    """

    @staticmethod
    def _task(hardware):
        task = FakeTask()
        task.config = {'hardware': hardware}
        task.target_node = 'node-a'
        task.target_storage = 'vmstorage'
        return task

    def test_the_hibernation_file_is_cleared_when_no_drivers_are_installed(self, monkeypatch):
        seen = {}

        def fake_injection(_target, view, node_exec=None, clear_hibernation_only=False):
            seen['clear_only'] = clear_hibernation_only
            seen['drivers_flag'] = getattr(view, 'install_virtio_drivers', None)
            return True

        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers', fake_injection)
        task = self._task('compatible')
        note = hyperv_xhm._inject_drivers_if_asked(task, FakeTarget(), 120, [],
                                                   {'generation': 2})
        assert note is None
        assert seen['clear_only'] is True
        assert seen['drivers_flag'] is False, 'the clean run must not ask for drivers'

    def test_the_driver_run_does_not_ask_for_the_hibernation_only_mode(self, monkeypatch):
        seen = {}

        def fake_injection(_target, view, node_exec=None, clear_hibernation_only=False):
            seen['clear_only'] = clear_hibernation_only
            view.log('COPIED vioscsi')
            return True

        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers', fake_injection)
        volumes = [{'index': 0, 'controller': hyperv_xhm.VIRTIO_CONTROLLER,
                    'volume': 'vmstorage:vm-120-disk-0'}]
        hyperv_xhm._inject_drivers_if_asked(self._task('virtio'), FakeTarget(), 120,
                                            volumes, {'generation': 2})
        assert seen['clear_only'] is False

    def test_a_linux_guest_is_not_searched_for_a_windows_hibernation_file(self, monkeypatch):
        """It has none, there is no NTFS to look in, and the run would install ntfs-3g on
        the node for nothing and end with NO_WINDOWS_DIR logged as a failed preparation."""
        called = []
        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers',
                            lambda *a, **kw: called.append(kw) or True)
        hyperv_xhm._inject_drivers_if_asked(
            self._task('compatible'), FakeTarget(), 120, [],
            {'generation': 2, 'ostype': 'l26'})
        assert called == [], 'a Linux guest was mounted looking for Windows'

    def test_a_guest_that_could_be_windows_still_is(self, monkeypatch):
        called = []
        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers',
                            lambda *a, **kw: called.append(kw) or True)
        for detail in ({'generation': 2}, {'ostype': 'win11'},
                       {'generation': 1, 'secure_boot_enabled': True},
                       {'generation': 1, 'vtpm_enabled': True}):
            called.clear()
            hyperv_xhm._inject_drivers_if_asked(
                self._task('compatible'), FakeTarget(), 120, [], detail)
            assert called, f'skipped a guest that could be Windows: {detail}'

    def test_a_node_that_cannot_be_reached_does_not_fail_the_migration(self, monkeypatch):
        """The disks are already copied. Discarding a finished transfer over a
        preparation step would throw away the expensive half of the run."""
        def boom(*_a, **_kw):
            raise RuntimeError('no route to the node')

        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers', boom)
        task = self._task('compatible')
        assert hyperv_xhm._inject_drivers_if_asked(task, FakeTarget(), 120, [],
                                                   {'generation': 2}) is None
        assert any('hibernation' in str(line).lower() for line in task.log_lines)


class TestAGuestWhoseDriverTheLoaderRefuses:
    """What happens when the drivers cannot be made boot-critical.

    Windows Server 2012 R2 and older get a virtio-win driver that Red Hat no longer has
    signed through Microsoft. Registering it as a boot driver produces a VM that stops at
    0xc0000428 before the kernel starts, which is worse than the SATA machine it would
    otherwise have been. The migration moves it back rather than handing that over.
    """

    @staticmethod
    def _volumes():
        return [{'index': 0, 'controller': hyperv_xhm.VIRTIO_CONTROLLER,
                 'volume': 'vmstorage:vm-120-disk-0'}]

    def _inject(self, monkeypatch, target, refuse=True):
        def fake_injection(_target, view, node_exec=None):
            view.log('COPIED vioscsi')
            if refuse:
                view.log('[VirtIO] BOOT_SIGNATURE_MISSING vioscsi')
            return not refuse

        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers', fake_injection)
        task = FakeTask()
        task.config = {'hardware': 'virtio'}
        task.target_node = 'node-a'
        task.target_storage = 'vmstorage'
        note = hyperv_xhm._inject_drivers_if_asked(task, target, 120, self._volumes(),
                                                   {'generation': 1})
        return task, note

    def test_the_disk_is_moved_off_the_controller_the_guest_cannot_boot_from(
            self, monkeypatch):
        target = FakeTarget()
        self._inject(monkeypatch, target)

        attached = [payload for _, payload in target.posts
                    if any(key.startswith('sata') for key in payload)]
        assert attached, 'the disk was never re-attached on the compatible controller'
        assert attached[0]['sata0'] == 'vmstorage:vm-120-disk-0'

    def test_the_virtio_disk_is_detached_before_it_is_re_attached(self, monkeypatch):
        target = FakeTarget()
        self._inject(monkeypatch, target)

        order = [payload for _, payload in target.posts]
        detach = next(i for i, p in enumerate(order) if 'delete' in p)
        attach = next(i for i, p in enumerate(order) if 'sata0' in p)
        assert detach < attach
        assert order[detach]['delete'] == 'scsi0'

    def test_the_vm_boots_from_the_disk_it_now_has(self, monkeypatch):
        target = FakeTarget()
        self._inject(monkeypatch, target)

        boot = [p['boot'] for _, p in target.posts if 'boot' in p]
        assert boot and boot[-1] == 'order=sata0'

    def test_the_operator_is_told_what_happened_and_why(self, monkeypatch):
        target = FakeTarget()
        _, note = self._inject(monkeypatch, target)

        assert 'no VirtIO driver' in note
        assert 'boots as it is' in note

    def test_nothing_is_moved_when_the_injection_succeeded(self, monkeypatch):
        target = FakeTarget()
        _, note = self._inject(monkeypatch, target, refuse=False)

        assert note is None
        assert not any('sata0' in payload for _, payload in target.posts)

    def test_any_failure_moves_the_vm_to_hardware_it_can_start_from(self, monkeypatch):
        """The reason does not change the outcome: no drivers on VirtIO means no boot.

        Measured on a real migration: the node could not install the injection's own
        dependencies, the VM was created on VirtIO with nothing written into it, and it
        was handed over in a state that cannot start.
        """
        def fake_injection(_target, view, node_exec=None):
            view.log('[VirtIO] ✗ apt install failed: ')
            return False

        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers', fake_injection)
        target = FakeTarget()
        task = FakeTask()
        task.config = {'hardware': 'virtio'}
        task.target_node = 'node-a'
        task.target_storage = 'vmstorage'

        note = hyperv_xhm._inject_drivers_if_asked(task, target, 120, self._volumes(),
                                                   {'generation': 1})
        assert 'could not be injected' in note
        assert any('sata0' in payload for _, payload in target.posts)

    def test_a_guest_that_is_not_windows_keeps_its_virtio_hardware(self, monkeypatch):
        """A Linux guest has VirtIO in its kernel and wants what it was given."""
        def fake_injection(_target, view, node_exec=None):
            view.log('[VirtIO] NO_WINDOWS_DIR')
            return False

        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers', fake_injection)
        target = FakeTarget()
        task = FakeTask()
        task.config = {'hardware': 'virtio'}
        task.target_node = 'node-a'
        task.target_storage = 'vmstorage'

        note = hyperv_xhm._inject_drivers_if_asked(task, target, 120, self._volumes(),
                                                   {'generation': 1})
        assert 'No Windows installation' in note
        assert not any('sata0' in payload for _, payload in target.posts)

    def test_a_volume_that_cannot_be_re_attached_is_named(self, monkeypatch):
        """After the detach it is on no controller at all, so it is gone from the VM.

        Stopping at the first failure would leave the rest of a multi-disk guest detached
        and unnamed, while the caller reported only that the controller could not be
        changed - which sends an operator looking for a disk on the wrong bus instead of
        for a disk that is missing.
        """
        target = FakeTarget()
        target.refuse_posts = ('sata1',)

        def fake_injection(_target, view, node_exec=None):
            view.log('[VirtIO] BOOT_SIGNATURE_MISSING vioscsi')
            return False

        monkeypatch.setattr('pegaprox.core.v2p._inject_virtio_drivers', fake_injection)
        task = FakeTask()
        task.config = {'hardware': 'virtio'}
        task.target_node = 'node-a'
        task.target_storage = 'vmstorage'
        volumes = [{'index': 0, 'controller': hyperv_xhm.VIRTIO_CONTROLLER,
                    'volume': 'vmstorage:vm-120-disk-0'},
                   {'index': 1, 'controller': hyperv_xhm.VIRTIO_CONTROLLER,
                    'volume': 'vmstorage:vm-120-disk-1'}]

        note = hyperv_xhm._inject_drivers_if_asked(task, target, 120, volumes,
                                                   {'generation': 1})

        # The one that could be attached still was, so the loop did not stop.
        assert any('sata0' in payload for _, payload in target.posts)
        assert any('attached to no controller' in line and 'vm-120-disk-1' in line
                   for line in task.log_lines)
        assert 'could not be moved back' in note


# ===========================================================================
# Two adapters are two adapters
# ===========================================================================

class TestTwoAdaptersAreTwoAdapters:
    """A VM with more than one network card must be mappable card by card.

    Reported from the wizard: with two adapters on the same Hyper-V switch, changing one
    bridge changed the other. Both rows read "Produktion-Vswitch", and both wrote to the
    same entry in the network map — because the key was the MAC or, failing that, the
    adapter's name, and neither distinguishes two cards on one VM. Hyper-V reports all
    zeroes for a MAC until the VM has started once, and every adapter is called "Network
    Adapter" unless somebody renamed it.

    The consequence is not cosmetic and is invisible in the result: both cards land on one
    bridge and one VLAN, so a guest can arrive reachable by the wrong people.
    """

    NO_MAC = [
        {'name': 'Network Adapter', 'mac_address': '000000000000',
         'mac_address_colons': '00:00:00:00:00:00', 'switch_name': 'Produktion-Vswitch'},
        {'name': 'Network Adapter', 'mac_address': '000000000000',
         'mac_address_colons': '00:00:00:00:00:00', 'switch_name': 'Produktion-Vswitch'},
    ]

    def test_adapters_without_a_mac_do_not_share_a_key(self):
        from pegaprox.core.hyperv_preflight import adapter_key

        keys = [adapter_key(a, i) for i, a in enumerate(self.NO_MAC)]

        assert keys[0] != keys[1], (
            'two adapters share one map entry, so the wizard cannot address them apart')
        assert len(set(keys)) == len(keys)

    def test_a_real_mac_is_still_the_key(self):
        """It is stable across a re-plan, which a position is not once an adapter is added."""
        from pegaprox.core.hyperv_preflight import adapter_key

        adapter = {'name': 'Network Adapter', 'mac_address': '00155D000001'}
        assert adapter_key(adapter, 3) == '00155D000001'

    def test_every_row_says_which_adapter_it_is(self):
        from pegaprox.core.hyperv_preflight import adapter_label

        labels = [adapter_label(a, i) for i, a in enumerate(self.NO_MAC)]

        assert labels[0] != labels[1], 'two rows an operator cannot tell apart'
        assert labels[0].startswith('#1') and labels[1].startswith('#2')

    def test_the_plan_gives_each_adapter_its_own_key(self, db, wired):
        source, _, _ = wired
        source.get_vm_disks_for_export = lambda vmid: {'data': {
            'disks': [], 'network_adapters': list(self.NO_MAC), 'generation': 2,
            'power_state': 'Off', 'checkpoint_count': 0, 'hyperv_guid': GUID}}

        plan = hyperv_xhm.plan_hyperv_to_pve(SOURCE, VMID, TARGET)
        networks = plan['source']['networks']

        assert len({n['network'] for n in networks}) == 2
        assert len({n['label'] for n in networks}) == 2

    def test_the_run_puts_each_adapter_where_it_was_mapped(self, db, wired):
        """The half that matters: two cards, two bridges, not one bridge twice."""
        from pegaprox.core.hyperv_preflight import adapter_key

        source, target, _ = wired
        source._detail['network_adapters'] = list(self.NO_MAC)
        keys = [adapter_key(a, i) for i, a in enumerate(self.NO_MAC)]

        _run(FakeTask(), network_map={keys[0]: 'vmbr1', keys[1]: 'vmbr9'})

        created = self._created(target)
        assert 'bridge=vmbr1' in created['net0']
        assert 'bridge=vmbr9' in created['net1'], (
            'the second adapter did not get its own bridge')

    @staticmethod
    def _created(target):
        return next(data for url, data in target.posts if url.endswith('/qemu'))


class TestTheMacArrivesUnchanged:
    """Whatever the source says the address is, that is what the target gets.

    Only the spelling changes: Hyper-V reports `00155D000001` and Proxmox refuses that
    outright. The bytes are the same, and a dynamic address is carried like any other —
    dynamic means Hyper-V picked it, not that it may be replaced.
    """

    def test_a_dynamic_mac_is_carried_over_byte_for_byte(self, db, wired):
        source, target, _ = wired
        source._detail['network_adapters'] = [{
            'name': 'Network Adapter', 'mac_address': '00155D0A1B2C',
            'mac_address_colons': '00:15:5d:0a:1b:2c', 'dynamic_mac': True,
            'switch_name': 'Produktion-Vswitch'}]

        _run(FakeTask(), network_map={'00155D0A1B2C': 'vmbr1'})

        net0 = next(data for url, data in target.posts
                    if url.endswith('/qemu'))['net0']
        assert 'macaddr=00:15:5d:0a:1b:2c' in net0, (
            f'the guest arrives under a different address: {net0}')

    def test_only_an_address_that_does_not_exist_is_left_to_the_target(self, db, wired):
        from pegaprox.core.hyperv_preflight import adapter_key

        source, target, _ = wired
        adapter = {'name': 'Network Adapter', 'mac_address': '000000000000',
                   'mac_address_colons': '00:00:00:00:00:00',
                   'switch_name': 'Produktion-Vswitch'}
        source._detail['network_adapters'] = [adapter]

        _run(FakeTask(), network_map={adapter_key(adapter, 0): 'vmbr1'})

        net0 = next(data for url, data in target.posts
                    if url.endswith('/qemu'))['net0']
        assert 'macaddr' not in net0
        assert 'bridge=vmbr1' in net0


def test_no_default_anywhere_deletes_the_source():
    """The one option that cannot be taken back must never arrive by omission.

    `start_after` defaulting to true is what made every Hyper-V migration fail, and it was
    recoverable — nothing had happened yet. A `remove_source` that arrived the same way
    would delete the rollback of a migration nobody asked to be irreversible.
    """
    import inspect
    import os

    from pegaprox.core import xhm

    task_source = inspect.getsource(xhm.XHMigrationTask.__init__)
    assert "self.config.get('remove_source', False)" in task_source, (
        'the shared task no longer defaults remove_source to off')

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, 'web', 'src', 'dashboard.js'), encoding='utf-8') as fh:
        web = fh.read()
    assert 'remove_source: true' not in web
    assert 'remove_source:true' not in web
