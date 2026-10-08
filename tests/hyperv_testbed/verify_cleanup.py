"""Ask a real Proxmox whether cleaning up a failed import actually removes the VM.

The unit tests drive this path against a double. They prove the code issues these calls,
in this order, under these conditions -- and that is most of what matters. What a double
cannot show is that a Proxmox really loses the VM afterwards, or that the ownership check
survives contact with a description field a real PVE stored and handed back.

Two cases, because the interesting half of this code is the refusal:

  1. A VM created the way the runner creates it, carrying this migration's mark, recorded
     as a failed migration's leftover. The cleanup must delete it, and the API must then
     say it is gone.
  2. A VM at a VMID this migration recorded, but whose description no longer names the
     migration -- the number was handed to somebody else's guest after the import failed.
     The cleanup must refuse, report it as kept, and leave the VM running.

Not covered here: freeing a leftover volume. That half goes over SSH from PegaProx to the
node with the cluster's own stored credentials (_free_leftover_volumes -> _connect_ssh),
which needs a key file or a password in the cluster config; an agent-held key is not
something this script can hand it. The command it would run, `pvesm free`, is measured
separately by verify_target.sh, which frees every volume it allocates.

Everything about a particular installation comes from the environment. The API token is
read once from PEGAPROX_VERIFY_TOKEN, split, handed to the manager and never printed --
not in a result, not in an error, not in a traceback.

    PEGAPROX_VERIFY_HOST     the node's address
    PEGAPROX_VERIFY_NODE     its node name
    PEGAPROX_VERIFY_VMID     a free VMID; VMID+1 is used for the second case
    PEGAPROX_VERIFY_TOKEN    user@realm!name=secret

Run it from a scratch directory: it creates a PegaProx database under ./config.
"""

import os
import uuid

REQUIRED = ('PEGAPROX_VERIFY_HOST', 'PEGAPROX_VERIFY_NODE',
            'PEGAPROX_VERIFY_VMID', 'PEGAPROX_VERIFY_TOKEN')


class _Task:
    """The attributes _create_target_vm reads off a migration task, and nothing else."""

    def __init__(self, migration_id, vm_name, node):
        self.id = migration_id
        self.vm_name = vm_name
        self.source_vmid = 1
        self.target_node = node
        self.network_map = {}
        self.config = {}

    def log(self, message):
        print(f'    task: {message}')


def main():
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print('missing: ' + ', '.join(missing))
        return 2

    host = os.environ['PEGAPROX_VERIFY_HOST']
    node_name = os.environ['PEGAPROX_VERIFY_NODE']
    vmid = int(os.environ['PEGAPROX_VERIFY_VMID'])
    foreign_vmid = vmid + 1

    from pegaprox.core.manager import PegaProxManager
    from pegaprox.models.tasks import PegaProxConfig
    from pegaprox.core import hyperv_db, hyperv_xhm
    from pegaprox.globals import cluster_managers

    # Split once, hand on, drop the names. Nothing below can print it by accident.
    token_user, _, token_secret = os.environ.pop('PEGAPROX_VERIFY_TOKEN').partition('=')
    target = PegaProxManager('verify-target', PegaProxConfig({
        'name': 'verify-target', 'host': host,
        'user': token_user, 'pass': token_secret,
    }))
    del token_user, token_secret

    if not target.connect_to_proxmox():
        print('FAIL: could not connect to the target cluster')
        return 1
    cluster_managers['verify-target'] = target
    print(f'  connected to node {node_name}')

    conn = hyperv_xhm._conn()
    base = f'https://{target.host}:{target.api_port}/api2/json/nodes/{node_name}/qemu'
    detail = {'generation': 2, 'memory_mb': 512, 'cpu_count': 1, 'network_adapters': []}
    failures = []

    def exists(this_vmid):
        return target._api_get(f'{base}/{this_vmid}/config').status_code == 200

    def record(migration_id, this_vmid):
        hyperv_db.create_migration(
            conn, source_cluster='verify-source', source_vm_guid=str(uuid.uuid4()),
            target_cluster='verify-target', target_node=node_name,
            migration_id=migration_id)
        hyperv_db.update_migration(conn, migration_id, target_vmid=this_vmid,
                                   status=hyperv_db.STATUS_FAILED)
        hyperv_db.record_created_resource(conn, migration_id, 'vm', str(this_vmid))

    def create(this_vmid, migration_id, name):
        outcome = hyperv_xhm._create_target_vm(
            _Task(migration_id, name, node_name), target, this_vmid, detail)
        if outcome is not True:
            raise RuntimeError(f'could not create VM {this_vmid}: {outcome}')

    # --- 1. what this migration created ---------------------------------------------
    print(f'\n=== A VM this migration created, at VMID {vmid} ===')
    mine = f'vfy{uuid.uuid4().hex[:5]}'
    create(vmid, mine, 'verify-guest')
    record(mine, vmid)
    print(f'  exists before:        {exists(vmid)}')

    unconfirmed = hyperv_xhm.cleanup_migration(mine)
    print(f'  without confirmation: {unconfirmed.get("error", "")[:60]}')
    if unconfirmed.get('success') or not exists(vmid):
        failures.append('an unconfirmed cleanup removed something')

    result = hyperv_xhm.cleanup_migration(mine, confirmed=True)
    for entry in (result.get('removed') or []):
        print(f'  removed:              {entry["kind"]} {entry["id"]} -- {entry["note"]}')
    for entry in (result.get('kept') or []):
        print(f'  kept:                 {entry["kind"]} {entry["id"]} -- {entry["note"]}')
    still_there = exists(vmid)
    print(f'  exists after:         {still_there}')
    if still_there:
        failures.append(f'the cleanup did not remove VM {vmid}')
    if not result.get('success'):
        failures.append(f'the cleanup reported failure: {result.get("error")}')
    left = hyperv_db.get_migration(conn, mine).get('created_resources') or []
    print(f'  still recorded:       {len(left)} resource(s)')
    if left:
        failures.append('the removed VM is still recorded as a leftover')

    # --- 2. the VMID belongs to somebody else now ------------------------------------
    print(f'\n=== A VM at VMID {foreign_vmid} that no longer carries the mark ===')
    other = f'vfy{uuid.uuid4().hex[:5]}'
    create(foreign_vmid, other, 'not-ours')
    target._api_post(f'{base}/{foreign_vmid}/config',
                     data={'description': 'somebody else put this here'})
    record(other, foreign_vmid)

    refusal = hyperv_xhm.cleanup_migration(other, confirmed=True)
    kept = refusal.get('kept') or []
    for entry in kept:
        print(f'  kept:                 {entry["kind"]} {entry["id"]} -- {entry["note"]}')
    survived = exists(foreign_vmid)
    print(f'  exists after:         {survived}')
    if not survived:
        failures.append('the cleanup deleted a VM that was not its own')
    if refusal.get('success') or not kept:
        failures.append('the cleanup did not refuse the foreign VM')
    left = hyperv_db.get_migration(conn, other).get('created_resources') or []
    if not left:
        failures.append('the refused VM was dropped from the leftover list anyway')

    response = target._api_delete(f'{base}/{foreign_vmid}')
    print(f'  removed it again:     HTTP {response.status_code}')

    print()
    if failures:
        for line in failures:
            print(f'  FAIL: {line}')
        return 1
    print('  The cleanup removed its own VM and refused the one that was not.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
