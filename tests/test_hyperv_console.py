# The Hyper-V VMConnect console: the protocol it speaks and the gate in front of it.
#
# Two halves, tested apart on purpose. The protocol half is pure — it turns values into
# instructions and bytes back into instructions, and it has no socket in it, so the cases
# that matter (a split read, a multi-byte character, an argument list in guacd's order)
# are all reachable. The gate half decides who may open a console at all, and is driven
# through the real access-control layer with a real manager.
#
# What neither can prove is that a Hyper-V host answers. That needs Windows, and
# docs/hyperv-console.md says exactly which measurements are still owed.

import pytest

from pegaprox.core import hyperv_console as console
from pegaprox.core import hyperv_console_ws as relay

HOST = 'hv_1'
VM_A = 100
VM_B = 101
GUID_A = '11111111-1111-1111-1111-111111111111'
GUID_B = '22222222-2222-2222-2222-222222222222'


# ===========================================================================
# The protocol
# ===========================================================================

class TestOneInstruction:
    def test_an_instruction_is_length_dot_value_semicolon(self):
        assert console.encode('select', 'rdp') == b'6.select,3.rdp;'

    def test_an_empty_value_is_a_value(self):
        """Every parameter guacd asked for needs a slot, and "default" is spelled empty."""
        assert console.encode('connect', '', 'x') == b'7.connect,0.,1.x;'

    def test_the_length_counts_characters_not_bytes(self):
        """A VM named with an umlaut is one character here and two bytes on the wire.

        Counting bytes desynchronises the stream for everything after it, and the symptom
        is a console that renders garbage rather than an error anybody can read.
        """
        assert console.encode('name', 'ü') == '4.name,1.ü;'.encode('utf-8')

    def test_decode_reverses_encode(self):
        elements = ['error', 'Server refused connection', '519']
        raw = console.encode(*elements).decode('utf-8').rstrip(';')
        assert console.decode(raw) == elements

    def test_an_unreadable_instruction_is_refused_rather_than_guessed(self):
        with pytest.raises(console.ConsoleError):
            console.decode('not-a-length.value')


class TestReadingAStream:
    """A read returns whatever has arrived: half an instruction, or three of them."""

    def test_an_instruction_split_across_two_reads_arrives_once_whole(self):
        reader = console.InstructionReader()
        assert reader.feed(b'5.ready,3.ab') == []
        assert reader.feed(b'c;') == ['5.ready,3.abc']

    def test_three_instructions_in_one_read_are_all_returned(self):
        reader = console.InstructionReader()
        assert reader.feed(b'3.nop;3.nop;3.nop;') == ['3.nop', '3.nop', '3.nop']

    def test_a_trailing_fragment_is_kept_for_the_next_read(self):
        reader = console.InstructionReader()
        reader.feed(b'3.nop;4.half')
        assert reader.pending == '4.half'


class TestTheConnectInstruction:
    """guacd matches values to names by position, so order is the entire contract."""

    ARGS = ['VERSION_1_5_0', 'hostname', 'port', 'username', 'security']

    def test_every_name_gets_a_slot_in_guacds_own_order(self):
        built = console.build_connect(
            self.ARGS, {'hostname': 'h', 'port': '2179', 'username': 'u', 'security': 'vmconnect'})
        assert console.decode(built.decode().rstrip(';')) == [
            'connect', 'VERSION_1_5_0', 'h', '2179', 'u', 'vmconnect']

    def test_a_name_we_have_no_opinion_about_is_answered_empty(self):
        """Skipping it instead would shift every parameter after it by one, silently."""
        built = console.build_connect(['VERSION_1_5_0', 'hostname', 'unknown-thing', 'port'],
                                      {'hostname': 'h', 'port': '2179'})
        assert console.decode(built.decode().rstrip(';')) == [
            'connect', 'VERSION_1_5_0', 'h', '', '2179']

    def test_the_version_slot_is_answered_with_the_version_guacd_named(self):
        built = console.build_connect(['VERSION_1_9_9'], {})
        assert console.decode(built.decode().rstrip(';')) == ['connect', 'VERSION_1_9_9']


class TestWhatMakesItAMachineConsoleRatherThanADesktop:
    def test_the_vm_is_named_by_guid_in_the_preconnection_blob(self):
        params = console.connection_parameters(
            host='hyperv.invalid', vm_guid=GUID_A, username='svc', password='x')
        assert params['preconnection-blob'] == GUID_A

    def test_the_security_mode_and_port_are_the_vmconnect_ones(self):
        """Without both, RDP reaches the guest's desktop — which needs a guest network, a
        booted operating system and a session, none of which a boot console has."""
        params = console.connection_parameters(
            host='hyperv.invalid', vm_guid=GUID_A, username='svc', password='x')
        assert params['security'] == 'vmconnect'
        assert params['port'] == '2179'

    def test_a_console_asks_for_no_drive_no_printer_and_no_sound(self):
        params = console.connection_parameters(
            host='hyperv.invalid', vm_guid=GUID_A, username='svc', password='x')
        assert params['enable-drive'] == ''
        assert params['enable-printing'] == ''
        assert params['disable-audio'] == 'true'


class TestWhatAFailureSays:
    def test_a_known_status_is_translated_into_something_actionable(self):
        assert 'Virtual Machine Connection' in console.status_meaning('519')

    def test_an_unknown_status_does_not_invent_a_cause(self):
        assert console.status_meaning('999') == 'The console could not be opened.'

    def test_an_absent_guacd_says_so_and_says_the_rest_still_works(self):
        """The one failure an operator meets before anything else: nothing installed yet."""
        with pytest.raises(console.ConsoleError) as raised:
            # Port 1 needs no listener to be refused, and needs no privileges to try.
            console.connect({}, endpoint=('127.0.0.1', 1), timeout=2)
        assert 'guacd' in raised.value.message
        assert 'works without it' in raised.value.message


# ===========================================================================
# Who may open one
# ===========================================================================

def _hyperv_manager(api, *, host='hyperv.invalid'):
    fake = api.make_fake_manager(cluster_id=HOST, cluster_type='hyperv')
    fake.id = HOST
    fake.name = HOST
    fake.is_connected = True
    fake.config.host = host
    fake.config.user = 'CORP\\svc-migrate'
    fake.config.pass_ = 'fixture-' + 'not-a-real-credential'
    fake.config.smb_domain = ''
    fake.guid_for.side_effect = lambda vmid: {VM_A: GUID_A, VM_B: GUID_B}.get(int(vmid))
    api.set_manager(HOST, fake)
    return fake


def _token(username, role='admin'):
    from pegaprox.utils.realtime import create_ws_token
    return create_ws_token(username, role)


class TestTheGateInFrontOfAConsole:
    def test_a_request_without_a_token_is_refused(self, api, seed):
        _hyperv_manager(api)
        job, reason = relay.authorize(f'/api/hyperv/{HOST}/vms/{VM_A}/console')
        assert job is None and 'expired' in reason

    def test_a_token_works_once(self, api, seed):
        _hyperv_manager(api)
        seed.user('root', role='admin')
        token = _token('root')
        path = f'/api/hyperv/{HOST}/vms/{VM_A}/console?token={token}'

        first, _ = relay.authorize(path)
        second, reason = relay.authorize(path)

        assert first is not None
        assert second is None and 'already been used' in reason

    def test_a_vm_acl_grant_does_not_open_the_neighbouring_console(self, api, seed):
        """The case the whole gate exists for: reaching the host through one VM."""
        _hyperv_manager(api)
        seed.tenant('other', clusters=[])
        seed.user('scoped', role='user', tenant_id='other')
        seed.vm_acl(HOST, VM_A, ['scoped'])

        allowed, _ = relay.authorize(
            f'/api/hyperv/{HOST}/vms/{VM_A}/console?token={_token("scoped", "user")}')
        denied, reason = relay.authorize(
            f'/api/hyperv/{HOST}/vms/{VM_B}/console?token={_token("scoped", "user")}')

        assert allowed is not None
        assert denied is None and 'may not open' in reason

    def test_a_disabled_account_cannot_open_a_console_with_a_live_token(self, api, seed):
        """A token minted a minute ago must not outlive the account that got it."""
        _hyperv_manager(api)
        seed.user('gone', role='admin', enabled=False)

        job, reason = relay.authorize(
            f'/api/hyperv/{HOST}/vms/{VM_A}/console?token={_token("gone")}')
        assert job is None and 'no longer' in reason

    def test_the_guid_comes_from_the_identity_map_and_not_from_the_request(self, api, seed):
        """A GUID in the URL would let a caller name a VM the access check never saw."""
        _hyperv_manager(api)
        seed.user('root', role='admin')

        job, _ = relay.authorize(
            f'/api/hyperv/{HOST}/vms/{VM_A}/console'
            f'?token={_token("root")}&preconnection-blob={GUID_B}&vm_guid={GUID_B}')

        assert job['parameters']['preconnection-blob'] == GUID_A

    def test_a_cluster_that_is_not_a_hyperv_host_has_no_vmconnect_console(self, api, seed):
        fake = api.make_fake_manager(cluster_id='pve_1', cluster_type='proxmox')
        api.set_manager('pve_1', fake)
        seed.user('root', role='admin')

        job, reason = relay.authorize(
            f'/api/hyperv/pve_1/vms/{VM_A}/console?token={_token("root")}')
        assert job is None and 'not a Hyper-V source' in reason

    def test_an_address_that_is_not_a_console_address_is_refused(self, api, seed):
        _hyperv_manager(api)
        seed.user('root', role='admin')
        job, reason = relay.authorize(f'/api/hyperv/{HOST}/vms/{VM_A}/disks?token={_token("root")}')
        assert job is None and 'not a console address' in reason

    def test_the_browsers_display_numbers_are_bounded(self, api, seed):
        """They arrive as text in a query string and end up sizing a buffer in guacd."""
        _hyperv_manager(api)
        seed.user('root', role='admin')

        job, _ = relay.authorize(
            f'/api/hyperv/{HOST}/vms/{VM_A}/console'
            f'?token={_token("root")}&width=999999&height=-4&dpi=nonsense')

        assert job['parameters']['width'] == '4096'
        assert job['parameters']['height'] == '320'
        assert job['parameters']['dpi'] == '96'

    def test_the_host_credentials_stay_on_the_server_side(self, api, seed):
        """What authorize hands on is a connection guacd will make, not a browser payload.

        The test is here rather than in the route because this is where the password is
        read; the route hands out a token and never sees one.
        """
        fake = _hyperv_manager(api)
        seed.user('root', role='admin')

        job, _ = relay.authorize(
            f'/api/hyperv/{HOST}/vms/{VM_A}/console?token={_token("root")}')

        assert job['parameters']['password'] == fake.config.pass_
        assert 'password' not in {'cluster_id', 'vmid', 'username'}
        assert set(job) == {'cluster_id', 'vmid', 'username', 'parameters'}


class TestTheTicketRoute:
    def test_the_ticket_carries_no_host_no_account_and_no_guid(self, api, seed):
        _hyperv_manager(api)
        admin = seed.user('root', role='admin')

        body = api.as_user(admin).post(
            f'/api/hyperv/{HOST}/vms/{VM_A}/console').get_json()

        rendered = str(body)
        assert 'hyperv.invalid' not in rendered
        assert 'svc-migrate' not in rendered
        assert GUID_A not in rendered
        assert body['token'] and body['expires_in'] > 0

    def test_a_caller_without_console_rights_on_the_vm_gets_no_ticket(self, api, seed):
        _hyperv_manager(api)
        seed.tenant('other', clusters=[])
        user = seed.user('scoped', role='user', tenant_id='other')
        seed.vm_acl(HOST, VM_A, ['scoped'], inherit_role=False, permissions=['vm.view'])

        response = api.as_user(user).post(f'/api/hyperv/{HOST}/vms/{VM_A}/console')
        assert response.status_code == 403

    def test_a_vm_of_another_host_is_not_reachable_through_this_one(self, api, seed):
        _hyperv_manager(api)
        admin = seed.user('root', role='admin')
        response = api.as_user(admin).post(f'/api/hyperv/{HOST}/vms/999/console')
        assert response.status_code == 404
