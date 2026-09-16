# How a VHDX is addressed and read from the target node.
#
# Everything in this module ends up on a command line on a customer's Proxmox node, built
# from values that came off a customer's Hyper-V host. These tests are mostly about that
# seam: a path that escapes the mounted share, a credential that would be visible in a
# process list, a format that would be probed rather than stated.
#
# The end-to-end proof — a read-only SMB export, a real cifs mount and a real qemu-img
# conversion whose output matches the source checksum — lives in the Docker testbed under
# tests/hyperv_testbed, because it needs a container and these do not.

import pytest

from pegaprox.core import hyperv_transfer as transfer
from pegaprox.core.hyperv_transfer import TransferError


class TestWhereTheFileIs:
    def test_a_drive_path_becomes_its_administrative_share(self):
        assert transfer.share_for('C:\\ClusterStorage\\vm\\a.vhdx') == (
            'C$', 'ClusterStorage/vm/a.vhdx')

    def test_a_mapped_drive_uses_the_share_the_operator_made(self):
        """The administrative share needs a local administrator on the source.

        Reading a disk file should not require that, so an operator who has made a
        dedicated read-only share can point a drive at it.
        """
        assert transfer.share_for('D:\\vm\\a.vhdx', {'D': 'hyperv-disks'}) == (
            'hyperv-disks', 'vm/a.vhdx')

    def test_a_lowercase_mapping_key_is_the_same_drive(self):
        assert transfer.share_for('E:\\a.vhdx', {'e': 'disks'})[0] == 'disks'

    @pytest.mark.parametrize('path', [
        '\\\\fileserver\\share\\a.vhdx',           # already a UNC path
        '\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\a.vhdx',
        'relative\\a.vhdx',
        '',
    ])
    def test_a_path_this_does_not_understand_is_refused_not_guessed(self, path):
        """Each of these needs a decision somebody makes.

        A fallback would quietly read a different file than the inventory named, and the
        migration would succeed with the wrong disk in it.
        """
        with pytest.raises(TransferError):
            transfer.share_for(path)

    @pytest.mark.parametrize('path', ['C:\\a\nrm -rf /\\b.vhdx', 'C:\\a\x00b.vhdx'])
    def test_a_path_holding_characters_a_path_cannot_hold_is_refused(self, path):
        with pytest.raises(TransferError):
            transfer.share_for(path)

    def test_a_relative_path_cannot_escape_the_mount(self):
        """The path comes from the source host. Treating it as trusted is how a share
        mount turns into a read of the node's own filesystem."""
        with pytest.raises(TransferError):
            transfer.source_file_path('/mnt/pegaprox-hyperv/ab12', '../../etc/shadow')

    def test_the_resolved_path_is_inside_the_mount(self):
        assert transfer.source_file_path('/mnt/pegaprox-hyperv/ab12', 'vm/a.vhdx') == \
            '/mnt/pegaprox-hyperv/ab12/vm/a.vhdx'

    def test_each_migration_mounts_somewhere_of_its_own(self):
        """Two migrations running at once must not be able to unmount each other."""
        assert transfer.mount_point_for('aaaa1111') != transfer.mount_point_for('bbbb2222')

    def test_a_migration_id_that_is_all_punctuation_is_refused(self):
        with pytest.raises(TransferError):
            transfer.mount_point_for('../..')


class TestTheAccountName:
    @pytest.mark.parametrize('given,expected', [
        ('CORP\\svc-migrate', ('svc-migrate', 'CORP')),
        ('svc-migrate@corp.invalid', ('svc-migrate', 'corp.invalid')),
        ('svc-migrate', ('svc-migrate', '')),
        ('', ('', '')),
    ])
    def test_both_windows_spellings_split_into_user_and_domain(self, given, expected):
        """mount.cifs wants the domain in its own field.

        Leaving it inside the username works against some servers and fails against others,
        which produces a failure that looks exactly like a wrong password.
        """
        assert transfer.split_account(given) == expected


class TestTheCommands:
    def test_the_password_never_reaches_a_command_line(self):
        """A password in a command is visible to every user on the node while it runs, and
        stays in the shell history and in anything auditing process starts."""
        secret = 'fixture-' + 'not-a-real-credential'
        command = transfer.credentials_file_command('/mnt/pegaprox-hyperv/x.credentials')
        content = transfer.credentials_file_content('svc', secret, 'CORP')

        assert secret not in command
        assert secret in content
        assert command.startswith('umask 077')

    def test_the_credentials_file_carries_the_domain_only_when_there_is_one(self):
        content = transfer.credentials_file_content('svc', 'x')
        assert 'domain=' not in content

    def test_the_mount_is_read_only(self):
        """The source is a customer's running hypervisor. This product has no business
        being able to write to it, and the mount option is where that is enforced."""
        command = transfer.mount_command('host.invalid', 'C$', '/mnt/x', '/mnt/x.cred')
        assert ',ro,' in ',' + ','.join(transfer.MOUNT_OPTIONS) + ','
        assert '-o' in command
        assert 'ro' in command

    def test_the_mount_options_keep_the_page_cache_out_of_it(self):
        """qemu-img reads the source with O_DIRECT, which a cifs mount only supports when
        it was mounted cache=none. The two settings are one decision, not two."""
        assert 'cache=none' in transfer.MOUNT_OPTIONS

    def test_unmounting_runs_every_part_even_when_one_fails(self):
        """A left-behind mount holds a connection open to a customer's host, and a
        left-behind credentials file is a password sitting on disk."""
        command = transfer.unmount_command('/mnt/x', '/mnt/x.cred')
        assert command.count(';') >= 3
        assert command.rstrip().endswith('true')
        assert 'rm -f' in command

    def test_the_source_format_is_stated_rather_than_probed(self):
        """Probing means a file whose header was crafted to look like something else
        decides how it is read. The format is already known from the inventory."""
        command = transfer.convert_command('/mnt/x/a.vhdx', '/dev/pve/vm-100-disk-0')
        assert '-f vhdx' in command
        assert '-O raw' in command

    def test_the_target_is_written_into_and_not_created(self):
        """`pvesm alloc` already made the volume; qemu-img must not try to make it again.

        On a file or a block device it would just write into what is there, so this was
        invisible for as long as nobody migrated onto Ceph. There the target is an `rbd:`
        URL, qemu-img creates the image behind it, and RBD answers
        `error rbd create: File exists` — the same way on every retry, because nothing
        about it is transient. Reported from a real migration onto a Ceph pool, three
        identical failures in twenty-two seconds.
        """
        command = transfer.convert_command(
            '/mnt/x/a.vhdx',
            'rbd:vm-pool/vm-100-disk-0:conf=/etc/pve/ceph.conf:id=admin')
        assert ' -n ' in command, (
            'without -n the conversion re-creates a volume that already exists, which '
            'Ceph refuses outright')

    def test_a_path_with_a_space_or_a_quote_reaches_the_shell_as_one_word(self):
        """Counting quotes proves nothing; splitting the command the way a shell would
        proves the file name arrives as a single argument and unchanged."""
        import shlex
        awkward = "/mnt/x/my disk's.vhdx"
        words = shlex.split(transfer.convert_command(awkward, '/dev/pve/vm-100-disk-0'))
        assert words[-2:] == [awkward, '/dev/pve/vm-100-disk-0']

    def test_a_path_that_would_be_two_words_cannot_become_two_commands(self):
        import shlex
        injected = "/mnt/x/a.vhdx; rm -rf /"
        words = shlex.split(transfer.convert_command(injected, '/dev/null'))
        assert words[-2] == injected

    def test_the_probe_asks_before_anything_is_allocated(self):
        """The commonest failure is a share that mounted but does not hold what the
        inventory said. Asking first turns that into a message rather than an orphan."""
        command = transfer.probe_command('/mnt/x/a.vhdx')
        assert command.startswith('test -r')


class TestReadingProgress:
    def test_the_last_percentage_in_a_chunk_is_the_current_one(self):
        """qemu-img rewrites one line with carriage returns, so a single read holds
        several updates and only the last one is now."""
        assert transfer.parse_progress('    (12.00/100%)\r    (37.50/100%)') == 37.5

    def test_output_with_no_percentage_reports_nothing_rather_than_zero(self):
        """Zero would move a progress bar backwards every time qemu-img said anything
        else, which reads as a transfer that restarted."""
        assert transfer.parse_progress('qemu-img: warning: something') is None
        assert transfer.parse_progress('') is None


# ===========================================================================
# The host-wide transport check
# ===========================================================================

class FakeNode:
    """A target node that records what it was asked to run."""

    def __init__(self, fail_on=None, rc=1, err=''):
        self.commands = []
        self.stdin = []
        self._fail_on = fail_on
        self._rc = rc
        self._err = err
        self.closed = False

    def run(self, command, stdin_data=None):
        self.commands.append(command)
        if stdin_data is not None:
            self.stdin.append(stdin_data)
        if self._fail_on and self._fail_on in command:
            return self._rc, '', self._err
        return 0, '', ''

    def close(self):
        self.closed = True


class FakeCheckConfig:
    user = 'CORP\\svc-migrate'
    pass_ = 'fixture-' + 'not-a-real-credential'
    smb_domain = ''
    smb_share_map = {}
    host = 'hv-admin.invalid'
    transfer_host = ''


class FakeCheckSource:
    def __init__(self, **config):
        self.config = FakeCheckConfig()
        for key, value in config.items():
            setattr(self.config, key, value)

    @property
    def transfer_address(self):
        return (self.config.transfer_host or '').strip() or self.config.host


def _check(source=None, node=None):
    from pegaprox.core import hyperv_transfer_check
    node = node or FakeNode()
    result = hyperv_transfer_check.run_check(
        source or FakeCheckSource(), object(), 'pve-1', lambda: node)
    return result, node


class TestWhichSharesAreProbed:
    def test_the_configured_map_is_what_a_migration_would_mount(self):
        from pegaprox.core import hyperv_transfer_check
        source = FakeCheckSource(smb_share_map={'C': 'VMS$', 'D': 'VMS$', 'E': 'BACKUP$'})
        # Deduplicated: two drives commonly map to one share, and mounting it twice would
        # measure the same thing twice and report two failures for one problem.
        assert hyperv_transfer_check.shares_to_probe(source) == ['BACKUP$', 'VMS$']

    def test_without_a_map_the_administrative_share_stands_in(self):
        from pegaprox.core import hyperv_transfer_check
        assert hyperv_transfer_check.shares_to_probe(FakeCheckSource()) == ['C$']


class TestWhatTheCheckDoes:
    def test_it_mounts_read_only_lists_and_unmounts(self):
        result, node = _check()
        assert result['ok'] is True
        mounts = [c for c in node.commands if 'mount -t cifs' in c]
        assert len(mounts) == 1
        assert ',ro,' in mounts[0] or mounts[0].count("'ro,") or 'ro,' in mounts[0]
        assert any(c.startswith('ls -1 ') for c in node.commands), 'the share was never read'
        assert any('umount' in c for c in node.commands), 'the share was left mounted'

    def test_the_password_never_reaches_a_command_line(self):
        # argv is readable in `ps` by anyone on the node.
        result, node = _check()
        assert FakeCheckConfig.pass_ not in ' '.join(node.commands)
        assert any(FakeCheckConfig.pass_ in blob for blob in node.stdin), \
            'the credentials were not sent over stdin at all'

    def test_it_uses_the_transfer_address_when_there_is_one(self):
        source = FakeCheckSource(transfer_host='hv-fast.invalid')
        result, node = _check(source)
        assert result['host'] == 'hv-fast.invalid'
        assert any('//hv-fast.invalid/' in c for c in node.commands)

    def test_a_node_without_cifs_is_named_as_the_node_s_problem(self):
        node = FakeNode(fail_on='mount -t cifs', err='mount: unknown filesystem type cifs')
        result, _ = _check(node=node)
        assert result['ok'] is False
        assert 'cifs-utils' in result['error']
        assert 'Hyper-V side' in result['error']

    def test_a_refused_account_is_named_as_the_host_s_problem(self):
        node = FakeNode(fail_on='mount -t cifs', err='mount error(13): Permission denied')
        result, _ = _check(node=node)
        assert 'may read this share' in result['error']

    def test_a_share_that_mounts_but_cannot_be_listed_is_still_a_failure(self):
        # The two are different facts, and only the second is what a transfer needs.
        node = FakeNode(fail_on='ls -1 ', err='Permission denied')
        result, _ = _check(node=node)
        assert result['ok'] is False
        assert 'could not be listed' in result['error']

    def test_the_session_is_closed_and_nothing_is_left_mounted_on_failure(self):
        node = FakeNode(fail_on='mount -t cifs', err='boom')
        _check(node=node)
        assert node.closed, 'the SSH session was left open'
        assert any('umount' in c for c in node.commands), 'cleanup did not run'

    def test_every_mapped_share_is_probed_not_just_the_first(self):
        # unmount_command also removes the credentials file, so unmounting share #1 with
        # it left share #2 mounting without one — reported as "the host refused the
        # account". Any host with a real share map has more than one.
        source = FakeCheckSource(smb_share_map={'C': 'VMS$', 'E': 'BACKUP$'})
        result, node = _check(source)
        assert result['ok'] is True, result['error']
        mounts = [c for c in node.commands if 'mount -t cifs' in c]
        assert len(mounts) == 2, f'only {len(mounts)} share(s) were probed'
        assert any('/VMS$' in c for c in mounts) and any('/BACKUP$' in c for c in mounts)

    def test_an_unreachable_address_is_not_called_a_refused_account(self):
        # mount.cifs says `error(113): No route to host`, and matching a bare '13' read
        # that as errno 13. It is the most likely failure of a separate transfer address.
        node = FakeNode(fail_on='mount -t cifs',
                        err='mount error(113): No route to host')
        result, _ = _check(node=node)
        assert 'TCP 445' in result['error']
        assert 'refused the account' not in result['error']

    def test_a_genuinely_refused_account_still_says_so(self):
        node = FakeNode(fail_on='mount -t cifs', err='mount error(13): Permission denied')
        result, _ = _check(node=node)
        assert 'may read this share' in result['error']

    def test_the_record_says_when_and_from_where(self):
        result, _ = _check()
        assert result['node'] == 'pve-1'
        assert result['at'] > 0 and result['at_text']
        assert result['shares'] == ['C$']
