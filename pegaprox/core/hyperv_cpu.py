"""Which CPU type an imported VM is given (fork issue #15).

Proxmox creates a VM on `kvm64` when nothing is said, a model older than any server the
guest is likely to have run on. The import suggests `x86-64-v3` instead, and
`x86-64-v2-AES` on a node whose processor cannot provide v3, because a VM configured
with a CPU model its host lacks does not start at all.

What the node can provide is read from `/nodes/{node}/status`, whose `cpuinfo.flags` is the
node's /proc/cpuinfo flag list. A node that does not answer gets the v2-AES suggestion: the
cost of that mistake is a slower guest, the cost of the other one is a VM that does not
start.
"""

import logging

logger = logging.getLogger(__name__)

DEFAULT_CPU_TYPE = 'x86-64-v3'
FALLBACK_CPU_TYPE = 'x86-64-v2-AES'

#: The models the wizard offers. `host` passes the node's own processor through, which is
#: fastest and ties the VM to nodes with that exact processor.
CPU_TYPES = ('x86-64-v2-AES', 'x86-64-v3', 'x86-64-v4', 'host', 'kvm64', 'qemu64')

#: What x86-64-v3 adds on top of v2, as Linux names the flags. LZCNT is reported as `abm`.
X86_64_V3_FLAGS = frozenset({'avx', 'avx2', 'bmi1', 'bmi2', 'f16c', 'fma', 'abm', 'movbe',
                             'xsave'})


def default_cpu_type(flags):
    """The suggestion for a node with these CPU flags, or with flags nobody could read."""
    if flags and X86_64_V3_FLAGS <= set(flags):
        return DEFAULT_CPU_TYPE
    return FALLBACK_CPU_TYPE


def node_cpu_flags(target, node):
    """The node's CPU flags as a set, or None when the node did not say."""
    try:
        response = target._api_get(
            f'https://{target.host}:{target.api_port}/api2/json/nodes/{node}/status')
        if response.status_code != 200:
            return None
        cpuinfo = ((response.json() or {}).get('data') or {}).get('cpuinfo') or {}
        flags = str(cpuinfo.get('flags') or '').split()
        return set(flags) or None
    except Exception:                                          # noqa: BLE001
        logger.debug('Could not read the CPU flags of %s', node, exc_info=True)
        return None


def node_cpu_choice(target, node):
    """What the wizard shows for a node: the suggestion and what it was based on."""
    flags = node_cpu_flags(target, node)
    return {
        'node': node,
        'default': default_cpu_type(flags),
        'flags_known': flags is not None,
        'supports_x86_64_v3': bool(flags) and X86_64_V3_FLAGS <= flags,
        'types': list(CPU_TYPES),
    }


def chosen_cpu_type(config, target, node):
    """The CPU type the VM is created with, and a line saying where it came from.

    An operator's choice outside the offered list is not passed on: the value goes into
    the create call as it stands, and Proxmox's own error would arrive after the disks
    have been converted.
    """
    chosen = str((config or {}).get('cpu_type') or '').strip()
    if chosen in CPU_TYPES:
        return chosen, f'CPU type {chosen}, as chosen'
    suggestion = default_cpu_type(node_cpu_flags(target, node))
    if chosen:
        return suggestion, (f'CPU type {chosen!r} is not one this import offers; using '
                            f'{suggestion}')
    return suggestion, f'CPU type {suggestion}, the suggestion for {node}'
