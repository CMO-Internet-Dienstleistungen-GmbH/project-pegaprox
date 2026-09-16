"""Can a target node read this Hyper-V host's disk share? Asked once, about the host.

Three things have to be true before any migration from a host moves a byte: the drive
holding the VHDX files is shared, the registered account may read that share, and the
target node has `cifs-utils` and a route to TCP 445. None of them is a property of the VM
somebody happens to be looking at — they are the same answer for every guest on the host.

The preflight used to ask this per VM, could not answer it, and so produced a warning to
confirm per VM. On an estate with 159 guests that is 159 confirmations of a question that
has one answer, and the twenty-first confirmation says nothing the first did not.

So it is measured instead: mount the share the way the transfer will, list it, unmount.
What comes back is stored on the host and the preflight reads it. A check that succeeded
turns the finding into a dated fact; a check that failed turns it into a blocker, because
a host nothing can read is a host nothing can be migrated from.

What this does NOT replace is the per-disk probe inside a migration. That one reads the
actual files of the actual VM, and it still decides.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from pegaprox.core import hyperv_transfer

logger = logging.getLogger(__name__)

#: Where the check mounts. Its own directory, so it cannot collide with a migration that
#: is running at the same time.
_MOUNT_POINT = '/mnt/pegaprox-hyperv/transfer-check'
_CREDENTIALS = '/mnt/pegaprox-hyperv/transfer-check.credentials'


def shares_to_probe(source) -> list[str]:
    """Which shares this host's migrations would actually mount.

    The configured map, when there is one — those are the shares an administrator set up
    for exactly this. With no map the transfer falls back to the administrative share of
    whichever drive a disk sits on, and `C$` stands in for that: it is the drive every
    Windows host has, and read access to it is decided by the same membership as the rest.
    """
    mapping = getattr(source.config, 'smb_share_map', None) or {}
    if mapping:
        # Deduplicated, because two drives commonly map to one share.
        return sorted({str(share) for share in mapping.values() if share})
    return [hyperv_transfer.ADMIN_SHARE_TEMPLATE.format(drive='C')]


def run_check(source, target, node_name, open_node) -> dict:
    """Mount each share read-only from `node_name`, list it, unmount. Never writes.

    `open_node` returns an object with `.run(command, stdin_data=None) -> (rc, out, err)`
    and `.close()` — the same connection the transfer itself uses, so this measures the
    path a migration would take rather than a second one that happens to look like it.
    The credentials go over stdin, never on a command line: argv is readable in `ps`.

    Returns the record stored on the host: whether it worked, which shares were tried,
    which node tried, when, and — when it failed — the message an operator acts on.
    """
    host = source.transfer_address
    shares = shares_to_probe(source)
    started = time.time()
    result = {
        'ok': False,
        'at': started,
        'at_text': datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M'),
        'node': node_name,
        'host': host,
        'shares': shares,
        'error': '',
    }

    user, domain = hyperv_transfer.split_account(source.config.user)
    domain = domain or getattr(source.config, 'smb_domain', '') or ''
    credentials = hyperv_transfer.credentials_file_content(user, source.config.pass_, domain)

    node = None
    try:
        node = open_node()
        rc, _out, err = node.run(
            f'mkdir -p {hyperv_transfer.MOUNT_ROOT} && '
            + hyperv_transfer.credentials_file_command(_CREDENTIALS),
            stdin_data=credentials)
        if rc != 0:
            result['error'] = f'Could not write the credentials file on {node_name}: {err or rc}'
            return result

        for share in shares:
            rc, _out, err = node.run(
                hyperv_transfer.mount_command(host, share, _MOUNT_POINT, _CREDENTIALS))
            if rc != 0:
                result['error'] = _explain(err or f'mount returned {rc}', host, share, node_name)
                return result

            # A mount that succeeded and a directory that can be read are two different
            # facts, and only the second is what a transfer needs.
            rc, _out, err = node.run(f'ls -1 {_MOUNT_POINT} >/dev/null')
            if rc != 0:
                result['error'] = (f'//{host}/{share} mounted on {node_name} but could not be '
                                   f'listed: {err or rc}. The account reaches the share and is '
                                   'not allowed to read it.')
                return result

            # Unmount WITHOUT the credentials path: `unmount_command` also removes the
            # file, and the next share in the loop would then mount without one and be
            # reported as an account the host refused. Any host with a real share map has
            # more than one, so this was the normal case.
            node.run(hyperv_transfer.unmount_command(_MOUNT_POINT, ''))
        result['ok'] = True
        return result
    except Exception as exc:                                     # noqa: BLE001
        result['error'] = str(exc)
        return result
    finally:
        # Whatever happened, nothing stays mounted and no password stays on the node.
        if node is not None:
            try:
                node.run(hyperv_transfer.unmount_command(_MOUNT_POINT, _CREDENTIALS))
            except Exception:                                    # noqa: BLE001
                logger.debug('Could not clean up after the transfer check on %s', node_name,
                             exc_info=True)
            try:
                node.close()
            except Exception:                                    # noqa: BLE001
                pass


def _explain(message: str, host: str, share: str, node: str) -> str:
    """Turn mount.cifs's output into the thing to go and fix.

    Its messages name a errno and a manual page; which of the three preconditions failed
    is derivable from them and is what an operator actually needs.
    """
    lowered = (message or '').lower()
    prefix = f'//{host}/{share} could not be mounted on {node}: '
    # Parenthesised: `A and B or C` binds as `(A and B) or C`, and the version without
    # them matched a bad share name as a missing kernel module.
    if ('unknown filesystem type' in lowered
            or 'no such device' in lowered
            or ('not found' in lowered and 'mount.cifs' in lowered)):
        return prefix + ('this node has no CIFS support. Install cifs-utils on it; nothing '
                         'on the Hyper-V side is wrong.')
    # Order and shape both matter: `'13' in lowered` also matches `mount error(113): No
    # route to host`, which reported an unreachable transfer address as a refused account
    # — the most likely failure of the separate-transfer-address feature, explained as the
    # one thing it is not.
    if 'no route' in lowered or 'unreachable' in lowered or 'timed out' in lowered:
        return prefix + (f'{node} cannot reach {host} on TCP 445. If a separate transfer '
                         'address is configured, that is the one being used here.')
    if ('permission denied' in lowered or 'access denied' in lowered
            or 'error(13)' in lowered or '(13)' in lowered):
        return prefix + ('the host refused the account. Check that the registered Hyper-V '
                         'account may read this share, and that an administrative share '
                         'has a local administrator behind it.')
    if 'not found' in lowered or 'bad network name' in lowered:
        return prefix + ('the host has no share by that name. Either create it, or name '
                         "the right one in this host's share map.")
    return prefix + message
