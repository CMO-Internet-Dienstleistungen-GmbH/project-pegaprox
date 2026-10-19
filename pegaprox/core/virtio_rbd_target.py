"""How the VirtIO driver injection reaches a boot disk on Ceph RBD (fork issue #46).

The injection script used to take pool and image from the PVE volume id,
`<storage>:vm-<vmid>-disk-0`. That id names the storage and carries no pool, so both came
out as the image name and `rbd map` failed on every Ceph target. What the node needs is in
the `pvesm path` result instead:

- a krbd=0 storage answers `rbd:<pool>/<image>:conf=…:id=…:keyring=…`, which says how to
  map it, including the credentials of a storage that is not the default client;
- a krbd=1 storage answers `/dev/rbd-pve/<fsid>/<pool>/<image>`, which PVE maps itself.

The URI is parsed here, in Python, with the same helpers the dd copy path uses (#722), and
the script only receives the finished command. Every value reaches the shell quoted.
"""

import re

_KRBD_PATH = re.compile(r'^/dev/rbd-pve/[^/]+/([^/]+)/([^/]+)$')

_MAP_AND_OWN = (
    '{indent}BLK=$({command} 2>&1 | tail -1)\n'
    '{indent}[ -b "$BLK" ] || {{ echo "RBD_MAP_FAILED: $BLK"; exit 2; }}\n'
    # The cleanup unmaps what RBD names, so only a map made here goes into it.
    '{indent}RBD="$BLK"\n'
)


def rbd_target_lines(vol_path):
    """The body of the injection script's `rbd)` branch for this `pvesm path` result.

    Sets BLK to the block device to open. Sets RBD only when the script mapped the image
    itself, because the cleanup unmaps whatever RBD names.
    """
    from pegaprox.core.v2p import _parse_rbd_uri, _rbd_map_command

    path = str(vol_path or '').strip()

    krbd = _KRBD_PATH.match(path)
    if krbd:
        pool, image = krbd.groups()
        # PVE's own map: used as it is and left in place, since unmapping it would pull
        # the disk out from under PVE. Only when the device node is missing is it mapped
        # here, from the pool and image the path names.
        return (
            '    if [ -b "$VOL" ]; then\n'
            '      BLK="$VOL"\n'
            '    else\n'
            + _MAP_AND_OWN.format(indent='      ',
                                  command=_rbd_map_command(pool, image, {}))
            + '    fi\n'
        )

    if path.startswith('rbd:'):
        pool, image, opts = _parse_rbd_uri(path)
        if pool and image:
            return _MAP_AND_OWN.format(indent='    ',
                                       command=_rbd_map_command(pool, image, opts))

    return "    echo 'RBD_PARSE_FAILED'; exit 2\n"
