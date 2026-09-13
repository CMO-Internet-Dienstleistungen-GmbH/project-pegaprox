"""A Hyper-V host, dressed as something PegaProx's cluster-shaped code can hold.

PegaProx keeps every hypervisor in `cluster_managers` and asks all of them the same
questions, most of which were written with Proxmox in mind. This adapter answers those
questions for a Hyper-V host, and answers them *completely* — the existing ESXi facade
answers only what cross-hypervisor migration needs, which makes the cluster list raise an
AttributeError the moment such a facade is registered.

Two translations happen here and nowhere else.

**Identity.** Hyper-V names a VM with a GUID; everything above this line — the API, the
access-control layer, the UI — takes an integer VMID and refuses or silently drops anything
else. The mapping lives in the database so it survives a restart, because an access-control
entry written against VMID 104 has to still mean the same VM tomorrow.

**Vocabulary.** A Hyper-V host is not a cluster and has no nodes, no shared storage and no
HA. Rather than inventing them, it presents itself as a single-node cluster whose one node
is the host, and reports the absent features as absent.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pegaprox.core import hyperv_db
from pegaprox.core.hyperv import HyperVManager
from pegaprox.core.hyperv_client import (
    DEFAULT_WINRM_HTTPS_PORT, HyperVConnection, PsrpHyperVClient,
)
from pegaprox.core.hyperv_errors import HyperVError, KIND_OK, KIND_UNKNOWN, remedy

logger = logging.getLogger(__name__)

# What the VM lists in PegaProx call a guest of this kind. Hyper-V VMs are full machines,
# so they are 'qemu' in the vocabulary of the lists that already exist, never 'lxc'.
_GUEST_TYPE = 'qemu'

# PegaProx's VM list uses these two words; Hyper-V has more states than that, and the
# mapping is deliberately coarse because the detail view carries the real one.
_RUNNING_STATES = frozenset({'Running', 'Starting'})


class HyperVConfig:
    """The settings object the rest of PegaProx expects to find on a manager.

    PegaProx reads a long list of Proxmox-shaped settings off `manager.config` — balancing
    thresholds, HA options, SSH details. None of them apply to a migration source, but
    several are read without a guard, so they exist here with honest values rather than
    being absent and raising in a cluster list nobody expected to break.
    """

    def __init__(self, data: dict):
        self.name = data.get('name') or data.get('host') or 'Hyper-V host'
        self.host = data.get('host', '')
        self.user = data.get('user', '')
        self.pass_ = data.get('pass', '') or data.get('pass_', '')
        # The shared cluster table stores a port under `api_port`; a freshly submitted form
        # sends `port`. Reading both is what keeps the WinRM port from silently reverting to
        # the default on the first restart after a host was configured on a custom one.
        self.port = int(data.get('port') or data.get('api_port') or DEFAULT_WINRM_HTTPS_PORT)
        self.ssl_verification = bool(data.get('ssl_verification', True))
        self.iso_library_paths = data.get('iso_library_paths') or []

        # How the target node reaches this host's disk files. Without a mapping a drive is
        # read through its administrative share, which works but asks for a local
        # administrator on the source where reading a file should have been enough.
        self.smb_share_map = data.get('smb_share_map') or {}
        self.smb_domain = data.get('smb_domain', '')

        # Read by the cluster list and the balancer without a getattr guard. A Hyper-V host
        # is never balanced and never a migration target, so every one of these is off.
        self.migration_threshold = 0
        self.migration_tolerance = 0
        self.check_interval = 0
        self.auto_migrate = False
        self.balance_containers = False
        self.balance_local_disks = False
        self.proxlb_tags_enabled = False
        self.dry_run = False
        self.enabled = bool(data.get('enabled', True))
        self.ha_enabled = False
        self.fallback_hosts = []
        self.excluded_nodes = []
        self.ha_settings = {}
        self.api_token_user = ''
        self.api_token_secret = ''
        self.vnc_tunnel = False
        self.node_ui_suffix = ''
        self.predictive_balancing = False
        self.predictive_threshold = 0
        self.balance_cpu_weight = 0
        self.balance_mem_weight = 0
        self.balance_io_weight = 0
        self.cpu_baseline = 0
        self.backup_sla_max_age_hours = 0
        self.api_port = self.port

        # SSH is how PegaProx reaches a Proxmox node. It never reaches a Hyper-V host that
        # way, and these exist only so code that reads them finds an empty value.
        self.ssh_user = ''
        self.ssh_key = ''
        self.ssh_port = 0

    def __repr__(self) -> str:
        # Never the password: config objects reach log lines and tracebacks.
        return f'HyperVConfig(name={self.name!r}, host={self.host!r}, port={self.port})'


class HyperVClusterManager:
    """One Hyper-V host, in the shape `cluster_managers` holds.

    Connects lazily and stays usable when the host is unreachable: a migration source that
    is down must show as disconnected in the cluster list, not prevent the list from being
    rendered.
    """

    cluster_type = 'hyperv'

    def __init__(self, cluster_id: str, config_data: dict, manager: HyperVManager | None = None):
        self.id = cluster_id
        self.cluster_id = cluster_id
        self.config = HyperVConfig(config_data)
        self.host = self.config.host
        # Read off the manager, not off its config, by the pages that build a Proxmox API
        # URL. Nothing here answers on it; it exists so those pages can branch instead of
        # raising before they get the chance.
        self.api_port = self.config.port
        self.logger = logger

        # PegaProx reads these on any manager; a host has no HA and no maintenance mode.
        self.ha_enabled = False
        self.ha_node_status = {}
        self.nodes_in_maintenance = []

        self.running = False
        self.connection_error = ''
        # When this host was last read. The cluster list renders it without a guard, and a
        # Hyper-V host is only read on demand, so it stays None until something asks.
        self.last_run = None
        # The load balancer's move log, which stays empty here: it lists the balancing
        # moves PegaProx made inside a cluster, and nothing balances a host it only reads
        # from. An attribute rather than a property, so a future writer is not surprised.
        self.last_migration_log = []
        # The classification is kept alongside the message so a caller can tell a wrong
        # password from a missing group membership from an untrusted certificate. The
        # message alone would collapse all three into one sentence about connecting.
        self.connection_kind = ''
        self.connection_remedy = ''
        self._manager = manager
        self._connected = False
        self._property_report: dict | None = None

    # -- lifecycle -------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _build_manager(self) -> HyperVManager:
        connection = HyperVConnection(
            host=self.config.host,
            username=self.config.user,
            password=self.config.pass_,
            port=self.config.port,
            verify_certificate=self.config.ssl_verification,
        )
        return HyperVManager(self.id, PsrpHyperVClient(connection), host=self.config.host)

    def connect(self) -> bool:
        """Reach the host once and record what it said.

        Also checks that the host's objects carry the properties this product reads. Those
        names are convention rather than documented contract, and an absent one would not
        raise — it would read as null and quietly make every VM look like it had no
        generation and no checkpoints.
        """
        try:
            if self._manager is None:
                self._manager = self._build_manager()
            facts = self._manager.host_facts()
            self._property_report = self._manager.verify_properties()
            self._connected = True
            self.running = True
            self.connection_error = ''
            self.connection_kind = KIND_OK
            self.last_run = datetime.now()
            self.connection_remedy = ''
            if not self._property_report.get('complete'):
                logger.warning(
                    'Hyper-V host %s does not expose every property PegaProx reads: %s',
                    self.id, self._property_report.get('missing'))
            logger.info('Connected to Hyper-V host %s (%s, PowerShell %s)',
                        self.id, facts.get('os_caption'), facts.get('powershell_version'))
            return True
        except HyperVError as exc:
            self._connected = False
            self.running = False
            # The classified message already has credentials and host names removed.
            self.connection_error = f'{exc.message} {exc.remedy}'.strip()
            self.connection_kind = exc.kind
            self.connection_remedy = exc.remedy
            logger.warning('Could not connect to Hyper-V host %s: %s', self.id, exc.kind)
            return False
        except Exception as exc:  # noqa: BLE001 - a source being down must not break the list
            self._connected = False
            self.running = False
            self.connection_error = str(exc)
            self.connection_kind = KIND_UNKNOWN
            self.connection_remedy = remedy(KIND_UNKNOWN)
            logger.exception('Unexpected error connecting to Hyper-V host %s', self.id)
            return False

    def start(self) -> None:
        """Registered for symmetry with the other managers; there is nothing to poll.

        A Hyper-V host is read when somebody asks. Background polling would add load to a
        customer's hypervisor for data that is only looked at during a migration.
        """
        self.connect()

    def stop(self) -> None:
        if self._manager is not None:
            self._manager.close()
        self._connected = False
        self.running = False

    @property
    def manager(self) -> HyperVManager:
        """The reading manager, connecting on first use."""
        if self._manager is None or not self._connected:
            self.connect()
        if self._manager is None:
            raise HyperVError(f'Not connected to Hyper-V host {self.id}.', kind='unreachable')
        return self._manager

    @property
    def property_report(self) -> dict | None:
        """What the connection check found, for the UI to surface."""
        return self._property_report

    # -- identity --------------------------------------------------------------------

    def vmid_for(self, vm_guid: str, vm_name: str = '') -> int:
        """The stable integer this VM is known by everywhere above this layer."""
        from pegaprox.core.db import get_db
        return hyperv_db.get_vmid(get_db().conn, self.id, vm_guid, vm_name)

    def guid_for(self, vmid) -> str | None:
        """Back from an integer VMID to the Hyper-V GUID, or None if it is not ours."""
        from pegaprox.core.db import get_db
        return hyperv_db.resolve_vmid(get_db().conn, self.id, vmid)

    # -- the questions PegaProx asks every manager -----------------------------------

    def get_vms(self) -> list[dict]:
        """Every VM, in the shape the VM lists and the migration wizard read."""
        vms = []
        for vm in self.manager.list_vms():
            guid = vm.get('guid')
            if not guid:
                # A VM with no identity cannot be addressed later, so listing it would only
                # offer somebody a row that fails when clicked.
                logger.warning('Skipping a Hyper-V VM without an id on host %s', self.id)
                continue
            vms.append({
                'vmid': self.vmid_for(guid, vm.get('name') or ''),
                'name': vm.get('name'),
                'status': 'running' if vm.get('state') in _RUNNING_STATES else 'stopped',
                'type': _GUEST_TYPE,
                'node': self.config.name,
                'maxmem': vm.get('memory_startup_bytes') or 0,
                'maxcpu': vm.get('cpu_count') or 0,
                'hyperv_state': vm.get('state'),
                'hyperv_guid': guid,
                'generation': vm.get('generation'),
            })
        return vms

    def get_vm_config(self, vmid) -> dict:
        """Full detail for one VM, addressed by its synthetic VMID."""
        guid = self.guid_for(vmid)
        if not guid:
            return {'error': f'No Hyper-V VM is known here as {vmid}.'}
        detail = self.manager.get_vm(guid)
        detail['vmid'] = int(vmid)
        return detail

    def get_nodes(self) -> list[dict]:
        """A host is a cluster of one, and says so rather than inventing nodes."""
        return [{'node': self.config.name, 'status': 'online' if self._connected else 'offline',
                 'type': 'node', 'id': f'node/{self.config.name}'}]

    def get_storages(self, node=None) -> list[dict]:
        """Hyper-V storage is not enumerable the way a Proxmox storage list is.

        The disks a migration reads are found through the VM that owns them, not through a
        host-wide storage list. Returning an empty list is the honest answer; inventing
        entries would put unusable options in a target picker.
        """
        return []

    def get_networks(self, node=None) -> list[dict]:
        """Virtual switches, named as the VMs' adapters refer to them."""
        switches = {vm.get('switch_name') for vm in self._all_adapters() if vm.get('switch_name')}
        return [{'iface': name, 'type': 'vswitch'} for name in sorted(switches)]

    def _all_adapters(self) -> list[dict]:
        """Every adapter on every VM, read once. Only used to enumerate switches."""
        adapters = []
        for vm in self.manager.list_vms():
            guid = vm.get('guid')
            if not guid:
                continue
            try:
                adapters.extend(self.manager.get_vm(guid).get('network_adapters') or [])
            except HyperVError:
                logger.debug('Could not read adapters of Hyper-V VM %s', guid, exc_info=True)
        return adapters

    def get_node_status(self) -> dict:
        """What the sidebar shows for this host.

        A Hyper-V host reports no CPU or memory figures here: reading them would mean
        polling a customer's hypervisor for numbers nothing in a migration uses.
        """
        return {self.config.name: {
            'status': 'online' if self._connected else 'offline',
            'cpu_percent': 0, 'mem_used': 0, 'mem_total': 0, 'mem_percent': 0,
            'uptime': 0, 'maintenance_mode': False, 'offline': not self._connected,
            'pveversion': '',
        }}

    def get_ha_status(self) -> dict:
        """No high availability on a migration source, in the shape the HA page reads.

        HA in PegaProx means watching Proxmox nodes and restarting guests elsewhere when
        one dies. A Hyper-V host is read from, never managed, so there is nothing to watch
        and nothing that would be allowed to act. `enabled: False` with no nodes is the
        truthful answer; the page renders it as "HA is off" instead of failing to load.
        """
        return {
            'enabled': False,
            'check_interval': 0,
            'failure_threshold': 0,
            'nodes': {},
            'recovery_in_progress': [],
            'fallback_hosts': [],
            'note': 'PegaProx does not manage high availability on a Hyper-V host. The '
                    'host is a migration source and is only read from.',
        }

    def get_pools(self) -> list[dict]:
        """Hyper-V has no resource pools, and says so rather than raising.

        The pools page is reachable for any cluster in the sidebar, including this one. An
        empty list renders as "no pools"; a missing method renders as a 500 on a page the
        operator opened for an unrelated reason.
        """
        return []

    def get_pool_members(self, pool_id) -> dict:
        """No pools means no members. Answered for the same reason as get_pools."""
        return {'members': []}

    def get_cluster_networks(self) -> dict:
        """The host's virtual switches, in the shape the network overview renders.

        A Hyper-V switch has no VLAN, no bridge ports and no cluster-wide identity, so the
        row carries the name and the VMs attached to it and nothing invented.
        """
        attached = {}
        for vm in self.get_vms():
            guid = vm.get('hyperv_guid')
            if not guid:
                continue
            try:
                adapters = self.manager.get_vm(guid).get('network_adapters') or []
            except HyperVError:
                logger.debug('Could not read adapters of Hyper-V VM %s', guid, exc_info=True)
                continue
            for adapter in adapters:
                switch = adapter.get('switch_name')
                if switch:
                    attached.setdefault(switch, []).append(
                        {'vmid': vm['vmid'], 'name': vm.get('name'),
                         'mac': adapter.get('mac_address')})

        return {'networks': [{'iface': name, 'type': 'vswitch', 'node': self.config.name,
                              'active': True, 'vms': vms, 'vm_count': len(vms)}
                             for name, vms in sorted(attached.items())],
                'nodes': [self.config.name]}

    def get_metric_servers(self) -> list[dict]:
        """A Hyper-V host does not push metrics anywhere through PegaProx."""
        return []

    def get_balancing_excluded_pools(self) -> list:
        """Nothing on a Hyper-V host is balanced, so nothing is excluded from balancing.

        The load balancer never touches a migration source: it has no pools, and moving a
        guest around a customer's hypervisor is not something this product does.
        """
        return []

    def get_cluster_fingerprint(self) -> dict:
        """No TLS fingerprint to publish, in the shape the caller checks.

        The fingerprint exists so a Proxmox node can be joined to a cluster, or so a
        remote migration can trust the far end. A Hyper-V host is neither, and returning
        something that looks like a fingerprint would invite exactly that. The failure
        shape matters: the caller reads `success` off the result, so a bare string turns
        a clear "not supported" into an AttributeError.
        """
        return {'success': False,
                'error': 'A Hyper-V host has no cluster fingerprint. It is a migration '
                         'source, not a cluster PegaProx can join or migrate into.'}

    def datacenter_status(self) -> dict:
        """What the overview page shows for this host.

        Deliberately without CPU, memory or storage figures. Producing them would mean
        polling a customer's hypervisor on every page load for numbers a migration never
        uses, and inventing zeroes would draw a graph that says the host is idle.
        """
        vms = self.get_vms()
        return {
            'cluster': {'name': self.config.name, 'quorate': None, 'standalone': True,
                        'version': 0, 'cluster_type': self.cluster_type},
            'nodes': self.get_nodes(),
            'vms': {'total': len(vms),
                    'running': len([vm for vm in vms if vm.get('status') == 'running']),
                    'stopped': len([vm for vm in vms if vm.get('status') != 'running'])},
            'resources': {
                'cpu': {'total': 0, 'used': 0, 'percent': 0},
                'memory': {'total': 0, 'used': 0, 'percent': 0},
                'storage': {'total': 0, 'used': 0, 'percent': 0},
            },
            'note': 'A Hyper-V host is a migration source. PegaProx reads the VMs it '
                    'can migrate and does not measure the host.',
        }

    # -- questions the shared cluster pages ask ------------------------------------------
    #
    # Everything below answers a question that only makes sense on a Proxmox cluster. The
    # answer is always the honest empty one, never an invented value, and it exists for a
    # single reason: a page an operator opens must render. Without these the shared route
    # raises an AttributeError and the browser gets a 500 on a page that has nothing to do
    # with migration. XCP-ng carries the same block for the same reason.

    def get_tasks(self, limit: int = 50) -> list:
        """No task log. Hyper-V jobs are the host's, and PegaProx does not adopt them.

        What PegaProx itself does to this host is a migration, and that is recorded in the
        migration table and shown on the migration page, not here.
        """
        return []

    def get_next_vmid(self) -> dict:
        """The next synthetic VMID this host would allocate, without allocating it.

        The number is real — it is the next entry in this host's own identity sequence —
        but it describes PegaProx's numbering of discovered VMs, not a free slot on the
        host. Nothing is created on a Hyper-V source through PegaProx.
        """
        from pegaprox.core.db import get_db
        try:
            return {'success': True,
                    'vmid': hyperv_db.peek_next_vmid(get_db().conn, self.id)}
        except Exception as exc:  # noqa: BLE001 — the caller renders the message
            logger.debug('Could not read the VMID sequence of %s', self.id, exc_info=True)
            return {'success': False, 'error': str(exc)}

    def get_replication_jobs(self, node=None) -> list:
        """Storage replication is a Proxmox ZFS feature. A Hyper-V host has none here."""
        return []

    def get_predictive_analysis(self) -> dict:
        """No forecast, because no measurements are taken.

        Predicting a node's load needs a history of that node's load. PegaProx deliberately
        does not poll a customer's hypervisor for figures a migration never uses, so there
        is nothing to extrapolate from and nothing that may be guessed.
        """
        return {}

    def get_proxmox_ha_groups(self) -> list:
        """Proxmox HA groups exist in a Proxmox cluster's config, which this is not."""
        return []

    def get_proxmox_ha_resources(self) -> list:
        """No guest here is under Proxmox HA management. Answered as an empty list."""
        return []

    def get_content_sync_status(self, content_type: str = 'iso') -> dict:
        """Which ISOs exist on which node, for a host that has exactly one node.

        The page compares content across the nodes of a cluster to show what is missing
        where. With a single host there is nothing to compare, and the ISO paths this
        patch does read are configured per host rather than enumerated per storage.
        """
        return {'nodes': [], 'files': [], 'matrix': {}}

    def _get_cpu_compatibility_matrix(self) -> dict:
        """Which VM may run on which node — a question with one node and no answer.

        The matrix exists so an operator sees before a live migration whether a guest's
        CPU model fits the target. Nothing live-migrates inside a Hyper-V source, and the
        CPU check that does matter for an import is part of the preflight.
        """
        return {'nodes': {}, 'vms': [], 'baseline': None}

    def get_vm_resources(self, max_age: float = 0.0) -> list[dict]:
        """The VM list, under the name the resource endpoints use.

        `max_age` is accepted and ignored: nothing is cached, so every answer is already
        as fresh as the parameter could ask for. The parameter exists because callers pass
        it positionally.
        """
        return self.get_vms()

    def get_vm_disks_for_export(self, vmid) -> dict:
        """The inventory read the migration planner performs.

        Shaped like the ESXi equivalent so the planner does not need a third branch, and
        carrying the Hyper-V specifics the mapping needs alongside.
        """
        guid = self.guid_for(vmid)
        if not guid:
            return {'error': f'No Hyper-V VM is known here as {vmid}.'}

        try:
            vm = self.manager.get_vm(guid)
        except HyperVError as exc:
            return {'error': exc.message, 'kind': exc.kind, 'remedy': exc.remedy}

        disks = [{
            'key': f'disk-{index}',
            'label': f"{disk.get('controller_type')} {disk.get('controller_number')}:"
                     f"{disk.get('controller_location')}",
            'path': disk.get('path'),
            'capacity_bytes': disk.get('size'),
            'capacity_gb': round((disk.get('size') or 0) / (1024 ** 3), 2),
            'file_size': disk.get('file_size'),
            'thin': disk.get('vhd_type') == 'Dynamic',
            'vhd_type': disk.get('vhd_type'),
            'parent_path': disk.get('parent_path'),
            'read_error': disk.get('read_error'),
            'target_controller_hint': disk.get('target_controller_hint'),
        } for index, disk in enumerate(vm.get('disks') or [])]

        return {'data': {
            'name': vm.get('name'),
            'power_state': vm.get('state'),
            'cpu_count': vm.get('cpu_count'),
            'memory_mb': vm.get('memory_mb'),
            'guest_os': '',
            'disks': disks,
            'total_disk_gb': round(sum(d['capacity_bytes'] or 0 for d in disks) / (1024 ** 3), 2),
            'generation': vm.get('generation'),
            'network_adapters': vm.get('network_adapters'),
            'checkpoint_count': vm.get('checkpoint_count'),
            'secure_boot_enabled': vm.get('secure_boot_enabled'),
            'vtpm_enabled': vm.get('vtpm_enabled'),
            'dynamic_memory_enabled': vm.get('dynamic_memory_enabled'),
            'hyperv_guid': guid,
        }}

    # -- explicitly absent -----------------------------------------------------------

    def create_migration_snapshot(self, vmid):
        """Not done here, and not silently either.

        A checkpoint taken to stabilise a migration would be one more differencing file to
        merge before the disks could be read — the opposite of what this migration needs.
        The source is prepared by shutting it down, which the facade does explicitly.
        """
        return {'error': 'A Hyper-V migration does not take a checkpoint of its source. '
                         'The VM is shut down instead.'}

    def delete_migration_snapshot(self, vmid):
        return {'error': 'No migration checkpoint is ever created on a Hyper-V source.'}


# =============================================================================
# What the application calls at boot
# =============================================================================

def register_hyperv_source(cluster_id: str, config_data: dict, managers: dict) -> None:
    """Put one configured Hyper-V host into the manager registry.

    Registration never waits for the host. A source that is switched off, unreachable or
    holding an expired certificate has to appear in the cluster list as disconnected —
    the operator needs to see the entry in order to fix it. Blocking here would instead
    delay every start-up by one connection timeout per unreachable host.
    """
    from pegaprox.core.db import get_db
    settings = hyperv_db.load_host_settings(get_db().conn, cluster_id)
    # The saved settings win over the shared cluster row for the fields they cover: the
    # shared row has no column for an ISO library and rounds the port through `api_port`,
    # so its values for those are the stale ones.
    merged = {**config_data, **{k: v for k, v in settings.items() if v is not None}}

    manager = HyperVClusterManager(cluster_id, merged)
    managers[cluster_id] = manager
    manager.start()
    logger.info('Registered Hyper-V source %s', cluster_id)


def sweep_interrupted_migrations() -> list[dict]:
    """Close the books on migrations that were running when this process last stopped.

    A migration row says 'running' for as long as something is writing to it. Nothing is
    writing to it after a crash or a restart, so without this the UI shows a transfer that
    will never move again and nobody can tell whether it is stuck or dead. Marking them
    interrupted is a statement about this process, not about the data: what those runs
    left on the target is cleaned up when somebody retries or discards them.
    """
    from pegaprox.core.db import get_db
    interrupted = hyperv_db.mark_interrupted_migrations(get_db().conn)
    if interrupted:
        logger.warning('Marked %d Hyper-V migration(s) as interrupted by a restart',
                       len(interrupted))
    return interrupted


def connect_hyperv_source(cluster_id: str, data: dict):
    """Build a Hyper-V source from submitted settings and prove it answers.

    Returns `(manager, None)` or `(None, error)`, where `error` is the classified failure
    dict every Hyper-V route already returns — so a wrong password, a missing group
    membership, an untrusted certificate and a host that is switched off stay four
    distinguishable answers instead of one "connection failed".

    Unlike start-up registration, this does wait for the host. Someone is sitting in front
    of the form, and saving a source that was never reachable only moves the failure to a
    place where it is harder to connect to what they just typed.

    The Hyper-V-only settings are written on success, because the shared cluster table has
    no column for them.
    """
    from pegaprox.core.db import get_db

    manager = HyperVClusterManager(cluster_id, data)
    if not manager.connect():
        return None, {
            'kind': manager.connection_kind,
            'message': manager.connection_error,
            'remedy': manager.connection_remedy,
        }

    hyperv_db.save_host_settings(
        get_db().conn, cluster_id,
        winrm_port=manager.config.port,
        verify_certificate=manager.config.ssl_verification,
        iso_library_paths=manager.config.iso_library_paths,
        smb_share_map=manager.config.smb_share_map,
        smb_domain=manager.config.smb_domain)
    return manager, None
