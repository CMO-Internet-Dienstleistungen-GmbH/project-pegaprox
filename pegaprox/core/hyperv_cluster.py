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
    DEFAULT_AUTH_METHOD, HyperVConnection, PsrpHyperVClient, default_winrm_port,
)
from pegaprox.core.hyperv_errors import (
    HyperVError, KIND_MISSING_FEATURE, KIND_OK, KIND_UNKNOWN, remedy,
)

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
        self.use_ssl = bool(data.get('use_ssl', False))
        self.port = int(data.get('port') or data.get('api_port')
                        or default_winrm_port(self.use_ssl))
        self.auth = data.get('auth') or DEFAULT_AUTH_METHOD
        self.encrypt_messages = bool(data.get('encrypt_messages', True))
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
            use_ssl=self.config.use_ssl,
            auth=self.config.auth,
            encrypt_messages=self.config.encrypt_messages,
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
                # The current figures, in the units the shared lists divide by: `cpu` is a
                # fraction of the VM's cores the way Proxmox reports it, not a percentage.
                # Omitting them is not neutral — the table renders an absent `mem` as
                # "NaN MB" beside a correct maximum.
                'mem': vm.get('memory_assigned_bytes') or 0,
                'cpu': (vm.get('cpu_usage_percent') or 0) / 100.0,
                'uptime': int(vm.get('uptime_seconds') or 0),
                'hyperv_state': vm.get('state'),
                'hyperv_guid': guid,
                'generation': vm.get('generation'),
            })
        return vms

    def vm_detail(self, vmid) -> dict:
        """Full detail for one VM, addressed by its synthetic VMID.

        The Hyper-V vocabulary, unchanged: this is what the migration planner and the
        Hyper-V endpoints read. `get_vm_config` below translates the same answer into the
        Proxmox-shaped envelope the shared VM dialog expects.
        """
        guid = self.guid_for(vmid)
        if not guid:
            return {'error': f'No Hyper-V VM is known here as {vmid}.'}
        detail = self.manager.get_vm(guid)
        detail['vmid'] = int(vmid)
        return detail

    def get_vm_config(self, node=None, vmid=None, vm_type='qemu') -> dict:
        """One VM for the shared VM dialog, in the shape and envelope it reads.

        The dialog is the first place an operator looks for a VM's hardware, and it asks
        every manager the same way — `get_vm_config(node, vmid, vm_type)`, then `success`
        and `config`. Without this the Hyper-V adapter raised a TypeError and the dialog
        told the operator to check their connection, which is a wrong diagnosis of a
        working one.

        Two positional forms reach here: the three-argument one above, and the
        `(None, vmid)` the cross-hypervisor planner uses. A single positional is the vmid,
        because a node name is a string and a synthetic VMID is not — guessing between the
        two is the kind of thing that fails far from where it was caused, so it is decided
        here and nowhere else.
        """
        if vmid is None:
            node, vmid = None, node
        detail = self.vm_detail(vmid)
        if 'error' in detail:
            return {'success': False, 'error': detail['error']}
        return {'success': True,
                'config': _proxmox_shaped_config(detail, self.config.name, vm_type)}

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
        nodes = self.get_nodes()
        online = len([node for node in nodes if node.get('status') == 'online'])
        running = len([vm for vm in vms if vm.get('status') == 'running'])
        # `nodes` is a tally and the guests live under `guests`, because that is the shape
        # the inventory overview reads — it takes `nodes.total` and `guests.vms.total`
        # straight from here. A node list and a top-level `vms` key were both truthful and
        # both rendered as "0 / 0", which is the one thing a migration source may not show.
        return {
            'cluster': {'name': self.config.name, 'quorate': None, 'standalone': True,
                        'version': 0, 'cluster_type': self.cluster_type},
            'nodes': {'online': online, 'offline': len(nodes) - online, 'total': len(nodes)},
            'guests': {'vms': {'running': running, 'stopped': len(vms) - running,
                               'total': len(vms)},
                       'containers': {'running': 0, 'stopped': 0, 'total': 0}},
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

    def get_node_summary(self, node=None) -> dict:
        """The host, in the shape the node summary card reads.

        Every figure the card would draw a number from is zero, and the note says why: the
        card's numbers come from `/nodes/<node>/status` on a Proxmox node, and PegaProx
        does not poll a customer's hypervisor for load it never acts on. Zero is what the
        card renders as "no data"; omitting the keys is what makes it render NaN.
        """
        return {
            'node': self.config.name,
            'status': 'online' if self._connected else 'offline',
            'uptime': 0,
            'cpu': 0,
            'cpuinfo': {},
            'memory': {'total': 0, 'used': 0, 'free': 0},
            'swap': {'total': 0, 'used': 0, 'free': 0},
            'rootfs': {'total': 0, 'used': 0, 'free': 0},
            'loadavg': [0, 0, 0],
            'kversion': '',
            'pveversion': '',
            'ksm': {'shared': 0},
            'maintenance_mode': False,
            'cluster_type': self.cluster_type,
            'note': 'A Hyper-V host is a migration source. PegaProx reads the VMs it '
                    'can migrate and does not measure the host.',
        }

    def get_node_rrddata(self, node=None, timeframe: str = 'hour') -> dict:
        """No history, in the shape the charts read.

        There is nothing to plot for the same reason get_node_summary carries zeroes:
        nothing here is measured over time. The empty series draw an empty chart, which is
        what an operator should see. A `success: False` would put an error banner on a page
        opened for an unrelated reason.
        """
        return {
            'timeframe': timeframe,
            'node': self.config.name,
            'timestamps': [],
            'metrics': {name: [] for name in
                        ('cpu', 'memory', 'swap', 'iowait', 'loadavg',
                         'net_in', 'net_out', 'rootfs')},
        }

    def get_node_network_config(self, node=None) -> list[dict]:
        """The host's virtual switches, as the network page's interface list.

        This is one of the few Proxmox-shaped questions that has a real Hyper-V answer: a
        virtual switch is what a VM's adapter is attached to, and the migration wizard maps
        it onto a target bridge. The Proxmox-only fields are left out rather than guessed —
        a switch has no CIDR, no gateway and no bridge ports.
        """
        return [{'iface': network['iface'], 'type': 'vswitch', 'method': 'manual',
                 'active': 1, 'autostart': 1}
                for network in self.get_networks()]

    def get_storage_list(self, node=None) -> list[dict]:
        """Storage, under the name the node and datacenter endpoints use.

        Same answer and same reason as get_storages: a Hyper-V disk is found through the VM
        that owns it, not through a host-wide list.
        """
        return self.get_storages(node)

    def get_vm_lock_status(self, node=None, vmid=None, vm_type='qemu') -> dict:
        """Nothing here is locked, in the shape the VM row reads.

        A Proxmox lock is a `lock:` line in the VM config that a running job puts there, so
        the UI can grey out actions instead of letting them fail. Hyper-V has no such line,
        and the source is not acted on from here anyway — the answer is no lock, not an
        error on a row the operator only wanted to look at.
        """
        return {'success': True, 'locked': False, 'lock_reason': None,
                'lock_description': None}

    def _get_node_ip(self, node=None):
        """The address of the one node, which is the host PegaProx was given.

        A Proxmox manager resolves this by scoring a node's interfaces, because a cluster
        node may be reachable at an address other than the one the cluster was registered
        under. A Hyper-V host has exactly one node and exactly one address, and that is it.
        """
        return self.config.host

    def _create_session(self):
        """Refuse to hand out a Proxmox REST session, and say why.

        About two hundred call sites build a `https://<host>:<port>/api2/json/...` URL on
        whatever this returns. A Hyper-V host serves none of them: it speaks WSMan on 5986
        and would answer every one of those paths with a 404 at best. Returning a session
        anyway would turn "this page does not apply here" into a page that loads slowly and
        then shows nothing, with no line anywhere saying what happened.

        Raising is safe because every one of those call sites already wraps the request in
        a try/except — they have to, since the far end is a network service.
        """
        raise HyperVError(
            'A Hyper-V host has no Proxmox REST API. This page reads one, so it does not '
            'apply to a migration source.',
            kind=KIND_MISSING_FEATURE)

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

#: A Hyper-V controller type as the Proxmox device prefix that carries the same meaning.
#: The mapping is for reading, never for placing a disk: what a migration actually attaches
#: the imported disk to is decided by the planner, not by where it sat on the source.
_CONTROLLER_PREFIX = {'IDE': 'ide', 'SCSI': 'scsi'}

#: Generation 2 is UEFI on a Q35 board; Generation 1 is BIOS on the default board. This is
#: the same equivalence the import itself applies, stated once.
_GENERATION_FIRMWARE = {1: ('seabios', ''), 2: ('ovmf', 'q35')}


def _device_id(disk, fallback_index: int) -> str:
    """`ide0`, `scsi1` — the source's own controller position, not an invented one.

    Falls back to a running index when the host did not report a position, so two disks
    can never collapse onto one row and hide one of them.
    """
    prefix = _CONTROLLER_PREFIX.get(disk.get('controller_type'), 'disk')
    location = disk.get('controller_location')
    return f'{prefix}{location if location is not None else fallback_index}'


def _proxmox_shaped_config(detail: dict, node: str, vm_type: str) -> dict:
    """A Hyper-V VM in the vocabulary the shared VM dialog reads.

    Every value here is translated, none is invented: a field with no Hyper-V counterpart
    is left empty rather than filled with a Proxmox default, because the dialog offers to
    edit what it shows and a default that looks like a setting is one somebody will try to
    change. The source is read-only — `read_only` says so, and the Hyper-V facts a
    migration actually turns on are carried under `hyperv` rather than squeezed into
    Proxmox keys that cannot hold them.
    """
    generation = detail.get('generation')
    bios, machine = _GENERATION_FIRMWARE.get(generation, ('', ''))

    disks = []
    for index, disk in enumerate(detail.get('disks') or []):
        disks.append({
            'id': _device_id(disk, index),
            'value': disk.get('path') or '',
            'storage': '',
            'volume': disk.get('path') or '',
            'size': disk.get('size'),
            'file_size': disk.get('file_size'),
            'format': disk.get('vhd_format'),
            'provisioning': disk.get('vhd_type'),
            'parent_path': disk.get('parent_path'),
            'read_error': disk.get('read_error'),
            'target_controller_hint': disk.get('target_controller_hint'),
        })

    networks = []
    for index, adapter in enumerate(detail.get('network_adapters') or []):
        networks.append({
            'id': f'net{index}',
            'value': adapter.get('switch_name') or '',
            'model': '',
            'macaddr': adapter.get('mac_address_colons'),
            'bridge': adapter.get('switch_name'),
            'dynamic_mac': adapter.get('dynamic_mac'),
            'connected': adapter.get('connected'),
            'vlan_mode': adapter.get('vlan_mode'),
            'tag': adapter.get('vlan_id'),
        })

    return {
        'general': {'name': detail.get('name') or '', 'description': '', 'tags': ''},
        'hardware': {
            'cores': detail.get('cpu_count') or 0,
            'sockets': 1,
            'cpu': '',
            'memory': detail.get('memory_mb') or 0,
            'balloon': 0,
            'bios': bios,
            'machine': machine,
            'scsihw': '',
        },
        'disks': disks,
        'networks': networks,
        'unused_disks': [],
        # onboot is Proxmox's word for the same thing Hyper-V calls AutomaticStartAction:
        # does this guest come back up by itself when the host does. Mapping it here rather
        # than adding a field means the shared VM dialog shows it without knowing about
        # Hyper-V at all. 'Nothing' is the only value that does not restart the guest.
        'options': {'onboot': 0 if (detail.get('automatic_start_action') or 'Nothing') == 'Nothing' else 1,
                    'boot': '', 'ostype': 'other', 'protection': 1},
        # What a migration has to reproduce or refuse, in the words the host used. None of
        # it has a Proxmox config key, and all of it decides whether an import succeeds.
        'hyperv': {
            'guid': detail.get('guid'),
            'state': detail.get('state'),
            # The exact values, because onboot can only say yes or no. StartIfRunning and
            # Start differ in when they fire, and that difference decides how much of a
            # risk an unfinished migration actually is.
            'automatic_start_action': detail.get('automatic_start_action') or '',
            'automatic_start_delay_seconds': detail.get('automatic_start_delay_seconds') or 0,
            'automatic_stop_action': detail.get('automatic_stop_action') or '',
            'generation': generation,
            'configuration_version': detail.get('configuration_version'),
            'secure_boot_enabled': detail.get('secure_boot_enabled'),
            'secure_boot_template': detail.get('secure_boot_template'),
            'vtpm_enabled': detail.get('vtpm_enabled'),
            'checkpoint_count': detail.get('checkpoint_count'),
            'checkpoints': detail.get('checkpoints') or [],
            'dynamic_memory_enabled': detail.get('dynamic_memory_enabled'),
            'memory_startup_bytes': detail.get('memory_startup_bytes'),
            'memory_minimum_bytes': detail.get('memory_minimum_bytes'),
            'memory_maximum_bytes': detail.get('memory_maximum_bytes'),
            'boot_order': detail.get('boot_order') or [],
            'dvd_drives': detail.get('dvd_drives') or [],
            'total_disk_bytes': detail.get('total_disk_bytes'),
            'bitlocker_state': detail.get('bitlocker_state'),
            'virtio_driver_state': detail.get('virtio_driver_state'),
        },
        # The source of a migration is never written to, so the dialog must not offer it.
        'read_only': True,
        'read_only_reason': 'This VM lives on a Hyper-V host that PegaProx only reads. '
                            'Change it in Hyper-V Manager, or migrate it to Proxmox.',
        'raw': detail,
        'status': {'status': 'running' if detail.get('state') in _RUNNING_STATES
                             else 'stopped'},
        'vmid': detail.get('vmid'),
        'node': node,
        'type': vm_type or _GUEST_TYPE,
    }


def migrate_hyperv_out_of_cluster_config() -> int:
    """Take Hyper-V hosts an earlier build left in the cluster configuration out of it.

    A host used to be registered as a cluster. It is a migration source now (ADR 1), kept
    in its own table, and nothing writes it to the cluster configuration any more - but
    nothing removed what was already there either, and an upgraded instance therefore
    carries the same host twice.

    The duplicate is not cosmetic. Start-up builds a manager for every entry in the cluster
    configuration, and anything that is not XCP-ng gets a PegaProxManager: a poll thread
    that logs in to a Proxmox API the host does not have, fails, and tries again with the
    zero interval an entry like this carries. Measured on an upgraded instance: about
    a hundred and twenty log lines a second, three gigabytes in twelve hours, and a full
    filesystem that then failed an unrelated migration with "No space left on device".
    The registry entry is replaced by the right manager a moment later, which leaves the
    thread running and no longer reachable to stop.

    Nothing is lost: an entry whose host is not in the host table yet is carried over
    first, and only then removed from the configuration.

    Returns how many were moved, so start-up can say it happened.
    """
    from pegaprox.core.config import load_config
    from pegaprox.core.db import get_db

    try:
        config = load_config() or {}
    except Exception as exc:                                   # noqa: BLE001
        logger.warning('Could not read the cluster configuration to migrate '
                       'Hyper-V sources out of it: %s', exc)
        return 0

    stale = {cluster_id: data for cluster_id, data in config.items()
             if (data or {}).get('cluster_type') == 'hyperv'}
    if not stale:
        return 0

    db = get_db()
    known = {record['id'] for record in hyperv_db.load_hosts(db.conn, db._decrypt)}
    moved = 0
    for cluster_id, data in stale.items():
        try:
            if cluster_id not in known:
                hyperv_db.save_host(db.conn, db._encrypt, cluster_id, data)
                logger.info('Carried Hyper-V host %s over from the cluster configuration',
                            cluster_id)
            db.delete_cluster(cluster_id)
            moved += 1
        except Exception as exc:                               # noqa: BLE001
            # One that cannot be moved must not stop the others, and must not be deleted
            # either: a host left in the configuration keeps working, badly, which is
            # better than a host that is gone.
            logger.warning('Could not move Hyper-V host %s out of the cluster '
                           'configuration: %s', cluster_id, exc)
    return moved


def load_hyperv_sources(managers: dict) -> int:
    """Register every saved Hyper-V host as a migration source.

    A Hyper-V host is a source, not a cluster (docs/adr/0001). It is therefore read from
    its own table and placed only into the manager registry — never into the cluster
    configuration, which is what the cluster list, the resource table and the datacenter
    overview are built from. This mirrors how an ESXi host becomes XHM-capable in app.py
    without becoming a cluster.

    Registration never waits for a host. One that is switched off, unreachable or holding
    an expired certificate has to appear as disconnected so the operator can see and fix
    it; blocking here would add one connection timeout per unreachable host to start-up.
    """
    from pegaprox.core.db import get_db

    db = get_db()
    registered = 0
    for record in hyperv_db.load_hosts(db.conn, db._decrypt):
        manager = HyperVClusterManager(record['id'], record)
        managers[record['id']] = manager
        manager.start()
        registered += 1
        logger.info('Registered Hyper-V source %s', record['id'])
    return registered


def register_hyperv_source(host_id: str, record: dict, managers: dict) -> HyperVClusterManager:
    """Put one host into the registry without touching the database."""
    manager = HyperVClusterManager(host_id, record)
    managers[host_id] = manager
    manager.start()
    return manager


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


def connect_hyperv_source(host_id: str, data: dict):
    """Build a Hyper-V source from submitted settings and prove it answers.

    Returns `(manager, None)` or `(None, error)`, where `error` is the classified failure
    dict every Hyper-V route already returns — so a wrong password, a missing group
    membership, an untrusted certificate and a host that is switched off stay four
    distinguishable answers instead of one "connection failed".

    Unlike start-up registration, this does wait for the host. Someone is sitting in front
    of the form, and saving a source that was never reachable only moves the failure to a
    place where it is harder to connect to what they just typed.
    """
    manager = HyperVClusterManager(host_id, data)
    if not manager.connect():
        return None, {
            'kind': manager.connection_kind,
            'message': manager.connection_error,
            'remedy': manager.connection_remedy,
        }
    return manager, None
