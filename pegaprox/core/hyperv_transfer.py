"""Getting a VHDX off a Hyper-V host and into a Proxmox volume.

The other cross-hypervisor directions in this product pull their disks through PegaProx:
the source offers an HTTP export, PegaProx reads it and pipes it into the target node.
Hyper-V offers nothing of the kind. Its disks are files, and the only sanctioned way to
read them from elsewhere is a file share.

That difference decides the shape of everything here.

**The data never passes through PegaProx.** The target node mounts the share read-only and
reads the file itself. Routing tens or hundreds of gigabytes through the management server
would make it the bottleneck and the single point of failure for a transfer that has
nothing to do with it.

**The conversion happens on the target, in one pass.** A dynamic VHDX is not a linear byte
stream — its payload is scattered across blocks described by a table the reader has to seek
back to. Piping one into a converter does not work, which is why the file has to be
reachable as a file rather than as a stream. `qemu-img convert` reads it and writes raw
straight into the allocated volume.

**A broken transfer restarts that disk.** There is no resume: the converter writes the
target in whatever order the source's block table dictates, so a byte count says nothing
about which parts of the target are valid. Pretending otherwise would produce a disk that
mounts and is quietly wrong, which is worse than copying it again.

Nothing here runs a command; the caller supplies the executor. That keeps the command
construction — the part that decides whether a credential lands in a process list —
testable without a Proxmox node and without a Hyper-V host.
"""

from __future__ import annotations

import logging
import posixpath
import re
import shlex

logger = logging.getLogger(__name__)

# Windows' own administrative share for a drive. Reaching a VHDX through it needs a local
# administrator on the Hyper-V host, which is more than reading a disk should require — so
# it is the fallback, and an operator who has made a dedicated read-only share is expected
# to map it instead.
ADMIN_SHARE_TEMPLATE = '{drive}$'

# Where the share is mounted on the target node. One directory per migration, so two
# migrations running at once cannot unmount each other's share.
MOUNT_ROOT = '/mnt/pegaprox-hyperv'

# mount.cifs options. `ro` is the important one: the source is a customer's running
# hypervisor and this product has no business being able to write to it. `noserverino`
# keeps inode numbers stable enough for a long sequential read; `vers=3.0` is the minimum
# every supported Windows Server speaks and avoids negotiating down to SMB1.
MOUNT_OPTIONS = ('ro', 'vers=3.0', 'noserverino', 'nobrl', 'cache=none')

# qemu-img's own source-format flag. Naming it is not optional: letting qemu-img probe the
# format means a file whose header was crafted to look like something else decides how it
# is interpreted, and the format is already known from the Hyper-V inventory.
SOURCE_FORMAT = 'vhdx'

_WINDOWS_PATH = re.compile(r'^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$')

# Anything that could end a shell word or start another one. Paths are quoted before they
# reach a command line, but a path containing these has no legitimate reading and is far
# more likely to be an attempt at one than a real file name.
_FORBIDDEN_IN_PATH = ('\n', '\r', '\x00')


class TransferError(Exception):
    """A transfer step that failed, with the operator-facing reason already in it."""


# ---------------------------------------------------------------------------
# Where the file is, seen from the target node
# ---------------------------------------------------------------------------

def split_windows_path(windows_path: str) -> tuple[str, str]:
    """`C:\\vm\\a.vhdx` as its drive letter and the rest, in POSIX form.

    Raises rather than guessing. A path this does not recognise is a UNC path, a volume
    GUID path or something malformed, and each of those needs a decision somebody makes
    rather than a fallback that silently reads the wrong file.
    """
    if not windows_path or any(bad in windows_path for bad in _FORBIDDEN_IN_PATH):
        raise TransferError('The disk path is empty or contains characters a path cannot hold.')

    match = _WINDOWS_PATH.match(windows_path.strip())
    if not match:
        raise TransferError(
            f'Cannot work out which share holds {windows_path!r}. Only a local drive path '
            'such as C:\\ClusterStorage\\vm\\disk.vhdx is understood; a UNC or volume-GUID '
            'path needs an explicit share mapping on the Hyper-V source.')
    return match.group('drive').upper(), match.group('rest').replace('\\', '/')


def share_for(windows_path: str, share_map: dict | None = None) -> tuple[str, str]:
    """The share name and the path within it for one disk file.

    `share_map` lets an operator point a drive at a dedicated read-only share instead of
    the administrative one — `{'C': 'hyperv-disks'}`. Without a mapping the administrative
    share is used, which works but asks for more rights on the source than reading a file
    should need.
    """
    drive, relative = split_windows_path(windows_path)
    share = (share_map or {}).get(drive) or (share_map or {}).get(drive.lower())
    if not share:
        share = ADMIN_SHARE_TEMPLATE.format(drive=drive)
    return share, relative


def mount_point_for(migration_id: str) -> str:
    """One mount directory per migration, so concurrent transfers stay independent."""
    safe = re.sub(r'[^A-Za-z0-9_-]', '', str(migration_id))[:32]
    if not safe:
        raise TransferError('A migration needs an id before its share can be mounted.')
    return posixpath.join(MOUNT_ROOT, safe)


def source_file_path(mount_point: str, relative_path: str) -> str:
    """The disk file as the target node will see it once the share is mounted."""
    resolved = posixpath.normpath(posixpath.join(mount_point, relative_path))
    # normpath collapses '..'; a relative path that escapes the mount point after that was
    # trying to read something outside the share.
    if not resolved.startswith(mount_point + '/'):
        raise TransferError(f'The disk path {relative_path!r} points outside the mounted share.')
    return resolved


# ---------------------------------------------------------------------------
# The commands the target node runs
# ---------------------------------------------------------------------------

def credentials_file_command(path: str) -> str:
    """Create the credentials file with no one but root able to read it.

    The content arrives on stdin, never in the command. A password in a command line is
    visible to every user on the node for as long as the process lives, and stays in the
    shell history and in the audit trail of anything watching process starts.
    """
    return f'umask 077 && cat > {shlex.quote(path)}'


def credentials_file_content(username: str, password: str, domain: str = '') -> str:
    """The `mount.cifs` credentials format. Written to a file, never to a command line."""
    lines = [f'username={username}', f'password={password}']
    if domain:
        lines.append(f'domain={domain}')
    return '\n'.join(lines) + '\n'


def mount_command(host: str, share: str, mount_point: str, credentials_path: str) -> str:
    """Mount the source share read-only under the migration's own mount point."""
    options = ','.join(MOUNT_OPTIONS) + f',credentials={credentials_path}'
    return (f'mkdir -p {shlex.quote(mount_point)} && '
            f'mount -t cifs {shlex.quote(f"//{host}/{share}")} {shlex.quote(mount_point)} '
            f'-o {shlex.quote(options)}')


def unmount_command(mount_point: str, credentials_path: str) -> str:
    """Undo the mount and remove the credentials, whether or not the copy worked.

    Written so every part runs even if an earlier one fails: a left-behind mount holds a
    connection open to a customer's host, and a left-behind credentials file is a password
    on disk.
    """
    quoted = shlex.quote(mount_point)
    return (f'umount {quoted} 2>/dev/null; '
            f'rmdir {quoted} 2>/dev/null; '
            f'rm -f {shlex.quote(credentials_path)}; true')


def convert_command(source_file: str, target_path: str) -> str:
    """Read the VHDX and write raw into the allocated volume.

    The source format is stated rather than probed, and `-p` makes qemu-img emit a
    percentage the caller can turn into progress. `-t none` and `-T none` keep the node's
    page cache out of the way: a multi-hundred-gigabyte copy would otherwise evict
    everything the guests already running on that node are using.

    `-T none` opens the source with O_DIRECT, which a CIFS mount only supports when it was
    mounted with `cache=none` — which is why that option is not negotiable in
    MOUNT_OPTIONS. Measured together against a read-only Samba export of a dynamic VHDX:
    the conversion completes and the result matches the source's checksum byte for byte.
    """
    return (f'qemu-img convert -p -f {SOURCE_FORMAT} -O raw -t none -T none '
            f'{shlex.quote(source_file)} {shlex.quote(target_path)}')


def probe_command(source_file: str) -> str:
    """Ask whether the file is there and readable before anything is allocated.

    Cheap, and it turns the most common failure — a share that mounted but does not hold
    what the inventory said — into a message before a volume exists to clean up.
    """
    return f'test -r {shlex.quote(source_file)} && stat -c %s {shlex.quote(source_file)}'


# ---------------------------------------------------------------------------
# Reading qemu-img's progress
# ---------------------------------------------------------------------------

_PROGRESS = re.compile(r'\((\d+(?:\.\d+)?)/100%\)')


def parse_progress(chunk: str) -> float | None:
    """The last percentage in a chunk of qemu-img output, or None if it holds none.

    qemu-img rewrites one line with a carriage return rather than printing new ones, so a
    read returns several updates at once and only the last is current.
    """
    matches = _PROGRESS.findall(chunk or '')
    if not matches:
        return None
    return float(matches[-1])


def split_account(username: str) -> tuple[str, str]:
    """An account name as (user, domain), whichever of the two Windows spellings was used.

    `DOMAIN\\user` and `user@domain` both reach a Hyper-V host, but `mount.cifs` wants the
    domain in its own field. Leaving it inside the username works against some servers and
    fails against others, which makes for a failure that looks like a wrong password.
    """
    if not username:
        return '', ''
    if '\\' in username:
        domain, _, user = username.partition('\\')
        return user, domain
    if '@' in username:
        user, _, domain = username.partition('@')
        return user, domain
    return username, ''
