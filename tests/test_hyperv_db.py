# Fork issue #15 — what has to outlive the process.
#
# Identity: Hyper-V names a VM with a GUID, while PegaProx's API and RBAC layer refuse or
# silently drop anything that is not an integer VMID. The mapping is therefore load-bearing
# for access control, and a renumbering after a restart would repoint every ACL written
# against the old numbers.
#
# Migration state: a migration creates real things on the target. The existing XHM engine
# keeps that in a process-local dict, so a restart mid-transfer leaves a half-built target
# with no record. These tests hold that the record survives and says what exists.
#
# Runs against the real SQLite schema through the `db` fixture — no host, no mocks.

import pytest

from pegaprox.core import hyperv_db

HOST_A = 'hyperv-host-a'
HOST_B = 'hyperv-host-b'
GUID_1 = '11111111-1111-1111-1111-111111111111'
GUID_2 = '22222222-2222-2222-2222-222222222222'


@pytest.fixture
def conn(db):
    """A connection whose schema includes the Hyper-V tables."""
    return db.conn


class TestIdentity:
    def test_the_schema_is_created_by_the_normal_startup_path(self, conn):
        # The tables come from db.py's own setup calling into hyperv_db, not from the test.
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert 'hyperv_vmid_map' in tables
        assert 'hyperv_migrations' in tables

    def test_the_same_vm_keeps_its_vmid(self, conn):
        first = hyperv_db.get_vmid(conn, HOST_A, GUID_1)
        second = hyperv_db.get_vmid(conn, HOST_A, GUID_1)
        assert first == second

    def test_two_vms_on_one_host_get_different_vmids(self, conn):
        assert hyperv_db.get_vmid(conn, HOST_A, GUID_1) != hyperv_db.get_vmid(conn, HOST_A, GUID_2)

    def test_the_same_guid_on_two_hosts_is_two_separate_vms(self, conn):
        # Each host is its own cluster_id, so the two mappings are independent. Without
        # this, a VM exported and imported onto a second host would collide with itself.
        a = hyperv_db.get_vmid(conn, HOST_A, GUID_1)
        b = hyperv_db.get_vmid(conn, HOST_B, GUID_1)
        assert hyperv_db.resolve_vmid(conn, HOST_A, a) == GUID_1
        assert hyperv_db.resolve_vmid(conn, HOST_B, b) == GUID_1

    def test_identically_named_vms_on_different_hosts_do_not_merge(self, conn):
        # Epic acceptance criterion: same VM name on two hosts must stay distinguishable.
        # Nothing resolves by name, which is what makes that true.
        hyperv_db.get_vmid(conn, HOST_A, GUID_1, vm_name='FILESERVER')
        hyperv_db.get_vmid(conn, HOST_B, GUID_2, vm_name='FILESERVER')
        assert hyperv_db.resolve_vmid(conn, HOST_A, hyperv_db.get_vmid(conn, HOST_A, GUID_1)) == GUID_1
        assert hyperv_db.resolve_vmid(conn, HOST_B, hyperv_db.get_vmid(conn, HOST_B, GUID_2)) == GUID_2

    def test_a_renamed_vm_keeps_its_vmid(self, conn):
        before = hyperv_db.get_vmid(conn, HOST_A, GUID_1, vm_name='OLD-NAME')
        after = hyperv_db.get_vmid(conn, HOST_A, GUID_1, vm_name='NEW-NAME')
        assert before == after

    def test_vmids_are_integers_because_everything_downstream_requires_one(self, conn):
        vmid = hyperv_db.get_vmid(conn, HOST_A, GUID_1)
        assert isinstance(vmid, int)
        assert int(vmid) == vmid

    def test_the_first_vmid_does_not_look_like_a_local_guest(self, conn):
        assert hyperv_db.get_vmid(conn, HOST_A, GUID_1) >= 100

    def test_resolving_an_unknown_vmid_is_none_not_an_error(self, conn):
        assert hyperv_db.resolve_vmid(conn, HOST_A, 999999) is None

    def test_resolving_a_non_numeric_vmid_is_none_not_an_error(self, conn):
        # The API layer passes whatever arrived in the URL.
        assert hyperv_db.resolve_vmid(conn, HOST_A, 'not-a-number') is None
        assert hyperv_db.resolve_vmid(conn, HOST_A, None) is None

    def test_a_vmid_is_never_reused_after_its_neighbour_is_removed(self, conn):
        # Reuse would hand a new VM the access-control entries of a deleted one.
        first = hyperv_db.get_vmid(conn, HOST_A, GUID_1)
        second = hyperv_db.get_vmid(conn, HOST_A, GUID_2)
        conn.execute('DELETE FROM hyperv_vmid_map WHERE cluster_id = ? AND vm_guid = ?',
                     (HOST_A, GUID_2))
        conn.commit()
        third = hyperv_db.get_vmid(conn, HOST_A, '33333333-3333-3333-3333-333333333333')
        assert third not in (first, second)

    def test_removing_a_host_drops_only_its_own_mappings(self, conn):
        hyperv_db.get_vmid(conn, HOST_A, GUID_1)
        hyperv_db.get_vmid(conn, HOST_B, GUID_2)
        removed = hyperv_db.forget_host(conn, HOST_A)
        assert removed == 1
        assert hyperv_db.resolve_vmid(conn, HOST_B, hyperv_db.get_vmid(conn, HOST_B, GUID_2)) == GUID_2


class TestMigrationRecord:
    def _migration(self, conn, **kw):
        params = dict(source_cluster=HOST_A, source_vm_guid=GUID_1, source_vm_name='synthetic-vm',
                      target_cluster='pve-1', target_node='node-1', target_storage='local-lvm')
        params.update(kw)
        return hyperv_db.create_migration(conn, **params)

    def test_the_record_exists_before_any_work_starts(self, conn):
        mid = self._migration(conn)
        row = hyperv_db.get_migration(conn, mid)
        assert row['status'] == hyperv_db.STATUS_RUNNING
        assert row['source_vm_guid'] == GUID_1

    def test_progress_and_phase_survive_being_written(self, conn):
        mid = self._migration(conn)
        hyperv_db.update_migration(conn, mid, phase='transfer', progress=42)
        row = hyperv_db.get_migration(conn, mid)
        assert row['phase'] == 'transfer'
        assert row['progress'] == 42

    def test_an_unknown_field_is_refused_rather_than_ignored(self, conn):
        # Column names would otherwise be interpolated into SQL from caller keys, and a
        # typo would silently write nothing.
        mid = self._migration(conn)
        with pytest.raises(ValueError):
            hyperv_db.update_migration(conn, mid, phse='transfer')

    def test_reaching_a_terminal_status_stamps_the_completion_time(self, conn):
        mid = self._migration(conn)
        hyperv_db.update_migration(conn, mid, status=hyperv_db.STATUS_COMPLETED)
        assert hyperv_db.get_migration(conn, mid)['completed_at'] is not None

    def test_created_resources_accumulate_in_order(self, conn):
        mid = self._migration(conn)
        hyperv_db.record_created_resource(conn, mid, 'vm', 'camera-shy-142', 'created on node-1')
        hyperv_db.record_created_resource(conn, mid, 'volume', 'local-lvm:vm-142-disk-0')
        resources = hyperv_db.get_migration(conn, mid)['created_resources']
        assert [r['kind'] for r in resources] == ['vm', 'volume']
        assert resources[0]['id'] == 'camera-shy-142'

    def test_recording_against_an_unknown_migration_is_an_error(self, conn):
        with pytest.raises(KeyError):
            hyperv_db.record_created_resource(conn, 'no-such-id', 'vm', 'x')

    def test_disk_progress_is_kept_per_disk(self, conn):
        mid = self._migration(conn)
        hyperv_db.set_disk_progress(conn, mid, 'disk-0', 500, 1000)
        hyperv_db.set_disk_progress(conn, mid, 'disk-1', 250, 1000)
        progress = hyperv_db.get_migration(conn, mid)['disk_progress']
        assert progress['disk-0']['pct'] == 50.0
        assert progress['disk-1']['copied'] == 250

    def test_disk_progress_of_a_zero_byte_disk_does_not_divide_by_zero(self, conn):
        mid = self._migration(conn)
        hyperv_db.set_disk_progress(conn, mid, 'disk-0', 0, 0)
        assert hyperv_db.get_migration(conn, mid)['disk_progress']['disk-0']['pct'] == 0.0


class TestRestart:
    def test_a_migration_left_running_is_reported_as_interrupted(self, conn):
        # This is the whole point of the table: after a crash there is no worker behind a
        # 'running' row, and saying so beats a migration that looks alive forever.
        mid = hyperv_db.create_migration(conn, source_cluster=HOST_A, source_vm_guid=GUID_1)
        hyperv_db.update_migration(conn, mid, phase='transfer', progress=40)

        interrupted = hyperv_db.mark_interrupted_migrations(conn)

        assert [row['migration_id'] for row in interrupted] == [mid]
        row = hyperv_db.get_migration(conn, mid)
        assert row['status'] == hyperv_db.STATUS_INTERRUPTED
        assert 'stopped' in row['error']

    def test_the_interrupted_record_still_lists_what_was_created(self, conn):
        # Without this list, cleaning up after a crash means guessing which target objects
        # belong to the dead migration.
        mid = hyperv_db.create_migration(conn, source_cluster=HOST_A, source_vm_guid=GUID_1)
        hyperv_db.record_created_resource(conn, mid, 'volume', 'local-lvm:vm-142-disk-0')
        hyperv_db.mark_interrupted_migrations(conn)
        resources = hyperv_db.get_migration(conn, mid)['created_resources']
        assert resources[0]['id'] == 'local-lvm:vm-142-disk-0'

    def test_a_finished_migration_is_left_alone_by_the_restart_sweep(self, conn):
        mid = hyperv_db.create_migration(conn, source_cluster=HOST_A, source_vm_guid=GUID_1)
        hyperv_db.update_migration(conn, mid, status=hyperv_db.STATUS_COMPLETED, progress=100)
        assert hyperv_db.mark_interrupted_migrations(conn) == []
        assert hyperv_db.get_migration(conn, mid)['status'] == hyperv_db.STATUS_COMPLETED

    def test_a_failed_migration_is_not_relabelled_as_interrupted(self, conn):
        mid = hyperv_db.create_migration(conn, source_cluster=HOST_A, source_vm_guid=GUID_1)
        hyperv_db.update_migration(conn, mid, status=hyperv_db.STATUS_FAILED,
                                   error='target storage was full')
        hyperv_db.mark_interrupted_migrations(conn)
        row = hyperv_db.get_migration(conn, mid)
        assert row['status'] == hyperv_db.STATUS_FAILED
        assert row['error'] == 'target storage was full'

    def test_migrations_are_listed_newest_first(self, conn):
        older = hyperv_db.create_migration(conn, source_cluster=HOST_A, source_vm_guid=GUID_1)
        newer = hyperv_db.create_migration(conn, source_cluster=HOST_A, source_vm_guid=GUID_2)
        listed = [row['migration_id'] for row in hyperv_db.list_migrations(conn)]
        assert listed.index(newer) < listed.index(older)

    def test_an_unreadable_json_column_does_not_hide_the_whole_row(self, conn):
        mid = hyperv_db.create_migration(conn, source_cluster=HOST_A, source_vm_guid=GUID_1)
        conn.execute("UPDATE hyperv_migrations SET created_resources = 'not json' "
                     "WHERE migration_id = ?", (mid,))
        conn.commit()
        row = hyperv_db.get_migration(conn, mid)
        assert row['created_resources'] == []
        assert row['source_vm_guid'] == GUID_1


class TestVmidsAreNeverRecycled:
    """The allocation counter only goes up, whatever happens to the rows.

    A recycled VMID silently hands a new VM every access-control entry written for the
    deleted one that held it. Deriving the next id from MAX(vmid) does exactly that, which
    is why the counter is stored separately.
    """

    def test_removing_and_re_adding_a_host_does_not_restart_the_numbering(self, conn):
        first = hyperv_db.get_vmid(conn, HOST_A, GUID_1)
        hyperv_db.forget_host(conn, HOST_A)
        after_readd = hyperv_db.get_vmid(conn, HOST_A, GUID_2)
        assert after_readd > first

    def test_ids_keep_climbing_across_many_add_and_remove_cycles(self, conn):
        seen = []
        for n in range(6):
            guid = f'{n}0000000-0000-0000-0000-000000000000'
            seen.append(hyperv_db.get_vmid(conn, HOST_A, guid))
            conn.execute('DELETE FROM hyperv_vmid_map WHERE cluster_id = ? AND vm_guid = ?',
                         (HOST_A, guid))
            conn.commit()
        assert seen == sorted(seen)
        assert len(set(seen)) == len(seen)

    def test_each_host_has_its_own_counter(self, conn):
        # One busy host must not push another host's numbering along with it.
        for n in range(4):
            hyperv_db.get_vmid(conn, HOST_A, f'{n}0000000-0000-0000-0000-00000000000a')
        assert hyperv_db.get_vmid(conn, HOST_B, GUID_1) == 100


class TestASchemaFromAnEarlierBuild:
    """CREATE TABLE IF NOT EXISTS leaves an existing table alone, including its old names."""

    def test_the_renamed_key_column_is_migrated(self, conn):
        conn.execute('DROP TABLE IF EXISTS hyperv_hosts')
        conn.execute('''
            CREATE TABLE hyperv_hosts (
                host_id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '', pass_encrypted TEXT DEFAULT '',
                winrm_port INTEGER DEFAULT 5986, verify_certificate INTEGER DEFAULT 1,
                iso_library_paths TEXT DEFAULT '[]', smb_share_map TEXT DEFAULT '{}',
                smb_domain TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)
        ''')
        conn.execute("INSERT INTO hyperv_hosts (host_id, name, host, created_at, updated_at) "
                     "VALUES ('h1', 'source', 'hv.invalid', 0, 0)")
        conn.commit()

        hyperv_db.ensure_schema(conn.cursor())
        conn.commit()

        columns = [row[1] for row in conn.execute('PRAGMA table_info(hyperv_hosts)').fetchall()]
        assert 'id' in columns and 'host_id' not in columns
        # The row survives the rename; a migration that loses registrations is worse than
        # the error it replaces.
        assert conn.execute('SELECT name FROM hyperv_hosts WHERE id = ?',
                            ('h1',)).fetchone()['name'] == 'source'

    def test_a_host_registered_before_the_transport_setting_stays_on_https(self, conn):
        """Until this column existed the transport was fixed: HTTPS with NTLM. A row from
        then was reached that way whatever its port, so the migration must say so
        explicitly -- the column default is HTTP, and a source silently switching to a
        listener it never used would fail on the first restart after the upgrade."""
        conn.execute('DROP TABLE IF EXISTS hyperv_hosts')
        conn.execute('''
            CREATE TABLE hyperv_hosts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '', pass_encrypted TEXT DEFAULT '',
                winrm_port INTEGER DEFAULT 5986, verify_certificate INTEGER DEFAULT 1,
                iso_library_paths TEXT DEFAULT '[]', smb_share_map TEXT DEFAULT '{}',
                smb_domain TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)
        ''')
        conn.execute("INSERT INTO hyperv_hosts (id, name, host, winrm_port, created_at, updated_at) "
                     "VALUES ('h1', 'default-port', 'hv.invalid', 5986, 0, 0)")
        conn.execute("INSERT INTO hyperv_hosts (id, name, host, winrm_port, created_at, updated_at) "
                     "VALUES ('h2', 'custom-port', 'hv2.invalid', 15986, 0, 0)")
        conn.commit()

        hyperv_db.ensure_schema(conn.cursor())
        conn.commit()

        for host_id in ('h1', 'h2'):
            record = hyperv_db.load_host(conn, lambda value: value, host_id)
            assert record['use_ssl'] is True, host_id
            assert record['auth'] == 'ntlm', host_id
            assert record['encrypt_messages'] is True, host_id
        assert hyperv_db.load_host(conn, lambda value: value, 'h2')['port'] == 15986

    def test_a_host_registered_after_the_transport_setting_gets_the_http_default(self, conn):
        hyperv_db.ensure_schema(conn.cursor())
        hyperv_db.save_host(conn, lambda value: value, 'h3',
                            {'name': 'new', 'host': 'hv3.invalid', 'user': 'svc'})
        record = hyperv_db.load_host(conn, lambda value: value, 'h3')
        assert record['use_ssl'] is False
        assert record['port'] == 5985
        assert record['auth'] == 'negotiate'
        assert record['encrypt_messages'] is True

    def test_the_transport_settings_survive_a_round_trip(self, conn):
        hyperv_db.ensure_schema(conn.cursor())
        hyperv_db.save_host(conn, lambda value: value, 'h4', {
            'name': 'plain', 'host': 'hv4.invalid', 'user': 'svc',
            'use_ssl': False, 'auth': 'basic', 'encrypt_messages': False,
        })
        record = hyperv_db.load_host(conn, lambda value: value, 'h4')
        assert (record['use_ssl'], record['auth'], record['encrypt_messages']) == (False, 'basic', False)

        hyperv_db.save_host(conn, lambda value: value, 'h5', {
            'name': 'tls', 'host': 'hv5.invalid', 'user': 'svc', 'use_ssl': True,
        })
        record = hyperv_db.load_host(conn, lambda value: value, 'h5')
        assert record['use_ssl'] is True
        assert record['port'] == 5986

    def test_a_current_schema_is_left_alone(self, conn):
        hyperv_db.ensure_schema(conn.cursor())
        hyperv_db.ensure_schema(conn.cursor())
        columns = [row[1] for row in conn.execute('PRAGMA table_info(hyperv_hosts)').fetchall()]
        assert 'id' in columns


class TestTheTransferCheckSurvivesTheRightThings:
    """A measurement about a host, and what saving the form does to it.

    `save_host` writes the whole row with INSERT OR REPLACE, so a column it does not name
    goes back to its default. The check was silently erased every time somebody pressed
    Save — found by the local UI harness, not by a unit test, which is why there is one now.
    """

    @staticmethod
    def _host(db, **overrides):
        data = {'name': 'src', 'host': 'hv.invalid', 'user': 'CORP\\svc', 'pass': 'p',
                'port': 5985, 'use_ssl': False, 'auth': 'negotiate',
                'encrypt_messages': True, 'ssl_verification': True,
                'iso_library_paths': [], 'smb_share_map': {}, 'smb_domain': '',
                'transfer_host': ''}
        data.update(overrides)
        return data

    def _saved(self, db, host_id='h1', password_submitted=False, **overrides):
        # Mirrors what `update_hyperv_host` sends: the stored password is always filled in
        # so the connection test can run, and `_password_submitted` says whether anybody
        # actually typed one.
        data = self._host(db, **overrides)
        data['_password_submitted'] = password_submitted
        hyperv_db.save_host(db.conn, db._encrypt, host_id, data)
        return hyperv_db.load_host(db.conn, db._decrypt, host_id)

    def test_a_plain_edit_keeps_it(self, db):
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True, 'node': 'pve-1'})
        # The same settings again, with no new password: nothing about the path changed.
        record = self._saved(db, name='renamed')
        assert record['transfer_check'].get('ok') is True

    def test_a_new_address_discards_it(self, db):
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True, 'node': 'pve-1'})
        record = self._saved(db, host='hv-other.invalid')
        assert record['transfer_check'] == {}

    def test_a_new_transfer_address_discards_it(self, db):
        # This is the address the check actually mounted from.
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True, 'node': 'pve-1'})
        record = self._saved(db, transfer_host='10.0.0.9')
        assert record['transfer_check'] == {}

    def test_a_new_account_discards_it(self, db):
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True, 'node': 'pve-1'})
        record = self._saved(db, user='CORP\\other')
        assert record['transfer_check'] == {}

    def test_a_new_password_discards_it(self, db):
        # Whether the share lets the account read is exactly what the check measured.
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True, 'node': 'pve-1'})
        record = self._saved(db, password_submitted=True, **{'pass': 'different'})
        assert record['transfer_check'] == {}

    def test_a_new_share_map_discards_it(self, db):
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True, 'node': 'pve-1'})
        record = self._saved(db, smb_share_map={'C': 'VMS$'})
        assert record['transfer_check'] == {}

    def test_recording_one_does_not_touch_the_password(self, db):
        # The two are written by different calls on purpose; a measurement must not be able
        # to rewrite a credential.
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True})
        assert hyperv_db.load_host(db.conn, db._decrypt, 'h1')['pass'] == 'p'

    def test_an_edit_that_carries_the_stored_password_forward_keeps_it(self, db):
        # The API fills `pass` from the existing record when the form left it blank, so a
        # plain rename arrives here WITH a password and used to discard the measurement.
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True, 'node': 'pve-1'})
        record = self._saved(db, name='renamed')          # same password, as the PUT sends it
        assert record['transfer_check'].get('ok') is True

    def test_a_share_map_in_a_different_key_order_is_not_a_change(self, db):
        self._saved(db, smb_share_map={'C': 'VMS$', 'E': 'BACKUP$'})
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True})
        record = self._saved(db, smb_share_map={'E': 'BACKUP$', 'C': 'VMS$'})
        assert record['transfer_check'].get('ok') is True

    def test_username_under_its_other_key_is_not_a_change(self, db):
        self._saved(db)
        hyperv_db.save_transfer_check(db.conn, 'h1', {'ok': True})
        data = self._host(db)
        data.pop('user')
        data['username'] = 'CORP\\svc'
        data['_password_submitted'] = False
        hyperv_db.save_host(db.conn, db._encrypt, 'h1', data)
        assert hyperv_db.load_host(db.conn, db._decrypt, 'h1')['transfer_check'].get('ok') is True
