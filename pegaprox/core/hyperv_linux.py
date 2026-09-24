"""Preparing a Linux guest for VirtIO on the target node, with virt-v2v.

Fork issue #15. A Linux guest that ran on Hyper-V boots from an initramfs that was built
for the hardware it had there: `hv_storvsc`, `hv_vmbus`, and on a hostonly build nothing
else. The VirtIO modules are in its kernel tree but not in that image, so the kernel comes
up and then cannot find its root device - on VirtIO and on SATA alike. What a guest like
that needs is not a driver copied in from outside, as for Windows, but its own initramfs
rebuilt with its own tools.

virt-v2v does exactly that, for every distribution it supports, and does the rest of the
work that goes with it: it picks the kernel, rebuilds the initramfs with the VirtIO
modules, adjusts the boot loader and device names, and relabels for SELinux. It runs in a
libguestfs appliance, so the guest's volume groups are never activated on the node.
`virt-v2v-in-place` does it on a disk that has already been copied, which is what the
Hyper-V import has at this point. Measured on a CentOS 7.7 guest on PVE 9.2: 191 s for a
150 GiB disk, and the VM booted to its login prompt on virtio-scsi.

Everything here builds commands and reads their output; running them is the caller's.
"""

import re
import shlex
from xml.sax.saxutils import quoteattr

#: What the node needs. `libguestfs-xfs` is only a Recommends of libguestfs, and the
#: install runs without recommends, so it is named: RHEL-family guests put /boot on XFS.
#: Without recommends, because supermin recommends a Debian kernel image, which a PVE node
#: must not be given. What the install does to the node regardless is documented in
#: docs/hyperv-target-node-requirements.md: mdadm is a hard dependency and rebuilds the
#: node's initramfs and boot configuration once.
PACKAGES = ('virt-v2v', 'libguestfs-xfs')

TOOL_PROBE = 'command -v virt-v2v-in-place >/dev/null 2>&1'

#: apt's own exit code, not that of a pipe's last stage. `apt-get ... | tail` reports
#: tail's success for an install that failed.
INSTALL_COMMAND = (
    'out=$(DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends '
    + ' '.join(PACKAGES) + ' 2>&1); rc=$?; printf "%s\\n" "$out" | tail -25; exit $rc')

#: The block driver the guest is converted for. The Hyper-V import builds a VirtIO VM on
#: virtio-scsi, and a guest converted for virtio-blk would look for /dev/vda on it.
BLOCK_DRIVER = 'virtio-scsi'

#: Every guest in this estate is run with guest-exec available, and RHEL-family packages
#: of qemu-guest-agent switch it off by default: RHEL 7 in BLACKLIST_RPC, later releases
#: in FILTER_RPC_ARGS, both in /etc/sysconfig/qemu-ga. Emptying the variable lifts the
#: filter. A guest without that file has no filter to lift. It runs before virt-v2v's own
#: SELinux relabel, so the rewritten file gets its label back.
GUEST_AGENT_UNLOCK = (
    "if [ -f /etc/sysconfig/qemu-ga ]; then "
    "sed -i -e 's/^BLACKLIST_RPC=.*/BLACKLIST_RPC=/' "
    "-e 's/^FILTER_RPC_ARGS=.*/FILTER_RPC_ARGS=/' /etc/sysconfig/qemu-ga; fi")

#: The name the SELinux module is installed under inside the guest. `semodule -l` lists it,
#: and `semodule -r pegaprox_qemu_ga_permissive` takes it out again.
SELINUX_MODULE = 'pegaprox_qemu_ga_permissive'

#: Every guest in this estate lets the node run commands through the guest agent, and on a
#: RHEL-family guest with SELinux enforcing the agent runs confined as virt_qemu_ga_t: it
#: may not run `ip`, write under /etc or talk to NetworkManager, so guest-exec answers but
#: cannot do its work. This marks that one domain permissive -- the rest of the guest stays
#: enforcing, and denials are still logged -- which gives the agent the rights it has on
#: every guest without SELinux. It is the module `semanage permissive -a` would write, as
#: CIL through `semodule`, because semanage is not installed on a minimal RHEL 7.
#: A guest with no SELinux configuration, or with SELinux disabled, is left alone. Any other
#: failure fails the command, and with it the conversion. It runs before virt-v2v's own
#: SELinux relabel.
GUEST_AGENT_SELINUX = (
    "if [ -f /etc/selinux/config ] && ! grep -qE '^SELINUX=disabled' /etc/selinux/config; "
    f"then printf '(typepermissive virt_qemu_ga_t)\\n' > /tmp/{SELINUX_MODULE}.cil "
    f"&& semodule -i /tmp/{SELINUX_MODULE}.cil && rm -f /tmp/{SELINUX_MODULE}.cil; fi")

#: How long a conversion may take. Measured 191 s for 150 GiB, most of it spent marking
#: unused areas; the ceiling leaves room for a guest several times that size.
CONVERSION_TIMEOUT = 3600

#: Written by the script so the caller can tell the stages apart without parsing prose.
MARK_EXIT = 'V2V_EXIT='
MARK_MAP_FAILED = 'RBD_MAP_FAILED'
MARK_UNMAP_FAILED = 'RBD_UNMAP_FAILED'

_RBD_OPTION = re.compile(r':(conf|id|keyring|mon_host)=([^:]*)')
_FILE_FORMATS = {'.raw': 'raw', '.qcow2': 'qcow2', '.img': 'raw'}


class UnsupportedVolume(ValueError):
    """A volume path this module does not know how to hand to virt-v2v."""


def disk_source(path: str) -> dict:
    """How the node reaches one volume, from what `pvesm path` printed for it.

    An RBD volume on a storage without krbd comes back as a QEMU URL, which virt-v2v
    cannot open, so it is mapped as a kernel block device first. Everything else is a
    path the node can open directly: a block device for LVM, ZFS and krbd, a file for the
    directory-based storages.
    """
    text = (path or '').strip()
    if text.startswith('rbd:'):
        body = text[len('rbd:'):]
        image = body.split(':', 1)[0]
        if not image or '/' not in image:
            raise UnsupportedVolume(f'Cannot read the pool and image from {text!r}')
        options = dict(_RBD_OPTION.findall(body[len(image):]))
        return {'kind': 'rbd', 'image': image, 'id': options.get('id', ''),
                'keyring': options.get('keyring', ''), 'conf': options.get('conf', '')}
    if text.startswith('/dev/'):
        return {'kind': 'block', 'path': text, 'format': 'raw'}
    if text.startswith('/'):
        suffix = text[text.rfind('.'):].lower() if '.' in text.rsplit('/', 1)[-1] else ''
        fmt = _FILE_FORMATS.get(suffix)
        if not fmt:
            raise UnsupportedVolume(f'{text} is neither a raw nor a qcow2 image')
        return {'kind': 'file', 'path': text, 'format': fmt}
    raise UnsupportedVolume(f'{text!r} is not a path the node can open')


def _rbd_map_command(source: dict) -> str:
    # `notrim`: virt-v2v 2.6 runs fstrim over every guest filesystem before converting,
    # to shrink a copy that an in-place run never makes, and it cannot be switched off
    # (`--no-trim` "now does nothing"). On an HDD-backed Ceph pool that trim discarded at
    # about 6 MB/s — measured on a 150 GiB guest, still trimming after 26 minutes, where
    # the same guest on NVMe took 150 s. A mapping without discard makes the trim fail at
    # once, which virt-v2v reports as a warning and continues past.
    parts = ['rbd', 'map', '-o', 'notrim']
    if source.get('id'):
        parts += ['--id', source['id']]
    if source.get('keyring'):
        parts += ['--keyring', source['keyring']]
    if source.get('conf'):
        parts += ['-c', source['conf']]
    parts.append(source['image'])
    return ' '.join(shlex.quote(part) for part in parts)


def _libvirt_disk(index: int, path_expr: str, kind: str, fmt: str) -> str:
    """One <disk> element. `path_expr` is a shell expression, expanded when the script runs."""
    target = 'sd' + 'abcdefghijklmnopqrstuvwxyz'[index]
    if kind == 'file':
        return (f"<disk type='file' device='disk'><driver name='qemu' type='{fmt}'/>"
                f"<source file=\"{path_expr}\"/><target dev='{target}' bus='scsi'/></disk>")
    return (f"<disk type='block' device='disk'><driver name='qemu' type='{fmt}'/>"
            f"<source dev=\"{path_expr}\"/><target dev='{target}' bus='scsi'/></disk>")


def conversion_script(sources: list[dict], guest_name: str = 'guest') -> str:
    """The script the node runs: map what needs mapping, convert, unmap on every path.

    One disk goes in as `-i disk`, the input mode measured on a real guest. Several go in
    as `-i libvirtxml`, because `-i disk` takes exactly one and a guest's root can span
    disks. Mapped RBD devices are released by a trap, so a failed conversion does not leave
    a kernel mapping of the guest's disk behind on the node.
    """
    if not sources:
        raise UnsupportedVolume('There is no disk to convert')
    if len(sources) > 26:
        raise UnsupportedVolume('More than 26 disks cannot be named for the conversion')

    # The unmap is retried: virt-v2v leaves on a signal before its nbdkit has let go of the
    # device, and one `rbd unmap` a moment later fails with EBUSY. Measured after a SIGTERM
    # on PVE 9.2: the device stayed mapped; unmapped by hand seconds later it went at once.
    lines = ['set -u', 'MAPPED=""', 'XML=""',
             'cleanup() { for d in $MAPPED; do '
             'for i in 1 2 3 4 5 6 7 8 9 10; do rbd unmap "$d" >/dev/null 2>&1 && break; '
             'sleep 3; done; '
             f'rbd showmapped 2>/dev/null | grep -q " $d\\$" && echo "{MARK_UNMAP_FAILED} $d"; '
             'done; [ -n "$XML" ] && rm -f "$XML"; }',
             'trap cleanup EXIT']
    exprs = []
    for index, source in enumerate(sources):
        var = f'DISK{index}'
        if source['kind'] == 'rbd':
            lines.append(f'{var}=$({_rbd_map_command(source)}) || '
                         f'{{ echo "{MARK_MAP_FAILED} {source["image"]}"; exit 3; }}')
            lines.append(f'MAPPED="$MAPPED ${var}"')
            exprs.append((var, 'block', 'raw'))
        else:
            lines.append(f'{var}={shlex.quote(source["path"])}')
            exprs.append((var, source['kind'], source['format']))

    common = (f'LIBGUESTFS_BACKEND=direct virt-v2v-in-place --block-driver {BLOCK_DRIVER} '
              f'--run-command {shlex.quote(GUEST_AGENT_UNLOCK)} '
              f'--run-command {shlex.quote(GUEST_AGENT_SELINUX)}')
    if len(exprs) == 1:
        var, _kind, fmt = exprs[0]
        lines.append(f'{common} -i disk -if {fmt} "${var}"')
    else:
        disks = ''.join(_libvirt_disk(i, f'${var}', kind, fmt)
                        for i, (var, kind, fmt) in enumerate(exprs))
        name = quoteattr(guest_name)[1:-1]
        lines.append('XML=$(mktemp /tmp/pegaprox-v2v-XXXXXX.xml)')
        lines.append(
            f'cat > "$XML" <<PEGAPROX_V2V_XML\n'
            f"<domain type='kvm'><name>{name}</name><memory unit='MiB'>1024</memory>"
            f"<vcpu>1</vcpu><os><type arch='x86_64'>hvm</type></os>"
            f'<devices>{disks}</devices></domain>\n'
            f'PEGAPROX_V2V_XML')
        lines.append(f'{common} -i libvirtxml "$XML"')
    lines.append('rc=$?')
    lines.append(f'echo "{MARK_EXIT}$rc"')
    lines.append('exit $rc')
    return '\n'.join(lines)


def read_result(output: str) -> dict:
    """What a conversion run printed, reduced to what the migration log needs.

    `exit` is virt-v2v's own code, None when the script ended before it ran. `lines` are
    the progress steps and virt-v2v's own messages, without the debugging chatter.
    """
    text = str(output or '')
    exit_code = None
    found = re.findall(rf'^{re.escape(MARK_EXIT)}(\d+)\s*$', text, re.M)
    if found:
        exit_code = int(found[-1])
    lines = [line.rstrip() for line in text.splitlines()
             if line.startswith(('[', 'virt-v2v', MARK_MAP_FAILED, MARK_UNMAP_FAILED,
                                 # What semodule said when the SELinux step failed.
                                 # Measured: virt-v2v's own error line only repeats the
                                 # command and "command exited with an error".
                                 'semodule', 'libsemanage'))]
    # virt-v2v names the failed command in its error line, and only this step's command
    # carries the module's type rule.
    selinux_failed = any(line.startswith('virt-v2v') and 'error:' in line
                         and 'typepermissive virt_qemu_ga_t' in line
                         for line in text.splitlines())
    return {'exit': exit_code, 'lines': lines, 'selinux_failed': selinux_failed,
            'map_failed': MARK_MAP_FAILED in text,
            'left_mapped': re.findall(rf'^{MARK_UNMAP_FAILED} (\S+)', text, re.M),
            'uefi': 'requires UEFI on the target' in text}
