"""Serving members (#625): a standby switched to serve users is an active instance to
them. They sign in there, see the clusters live and open consoles, shells and SPICE
there; every change goes to the leader (tests/test_ha_forward.py). Its role stays
standby, so nothing that acts on its own starts there, and tests/test_ha_loop_gates.py
holds as it is.

Runs on the in-process group of tests/test_ha_members.py with the forwarding of
tests/test_ha_forward.py: a is the leader, b its standby. Every console path is taken
both ways on b: opened here while b serves users, refused (409, nothing forwarded)
while it does not. A standby pins no SSH host key of its own, whether it serves or not.

MK Oct 2026
"""
import asyncio
import json
import subprocess
import types
from urllib.parse import parse_qsl, urlsplit

import pytest

from test_ha_api import _admin, _audit, _local_user, ADMIN_PW  # noqa: F401
from test_ha_members import group, _built, _post, _send, _sync, _watch, IDS, URLS  # noqa: F401
from test_ha_forward import fwd, probe, _forward_calls, _hook_lists, _concrete, FORWARD  # noqa: F401
from test_ha_v2_surface import (CID, VM, SHELL, STANDBY_ANSWER, _registry, _fake_manager,
                                _console_calls, _SyncWS, _AsyncWS, _sock_handler,
                                _standalone_vnc_handler, _auth_query, _ssh_server, _Resp)

VNC_SOCKET = '/api/clusters/<cluster_id>/vms/<node>/<vm_type>/<int:vmid>/vncwebsocket'
NOT_KNOWN = 'is not known here yet - open a shell to it on the leader once'


def _serve(g, n='b', on=True):
    with g.at(n) as ha:
        ha.set_serve_users(on)


def _api_token():
    from pegaprox.utils.auth import create_api_token
    return {'Authorization': f"Bearer {create_api_token('root', 'automation', role='admin')['token']}"}


def _members(g, admin, n):
    with g.at(n):
        return {m['instance_id']: m for m in admin.get('/api/ha/status').get_json()['members']}


# --- the switch ----------------------------------------------------------------------------

def test_the_switch_is_off_until_an_admin_turns_it_on(fwd, seed):
    g = fwd
    admin = _built(g, seed, 'b')
    with g.at('b') as ha:
        assert (ha.serve_users(), ha.serving(), ha.consoles_here()) == (False, False, False)
        assert ha.set_serve_users(True) is True
        assert ha.set_serve_users(True) is False
        assert (ha.serve_users(), ha.serving(), ha.consoles_here()) == (True, True, True)
        with pytest.raises(ha.HaError):
            ha.set_serve_users('yes')
        # still a standby: nothing that acts on its own starts here
        assert ha.is_standby() and ha.is_active() is False and ha.managers_wanted() is True
    assert g.file('b')['serve_users'] is True and 'serve_users' not in g.file('a')

    # this instance's own: the leader switching it on changes nothing on b
    _serve(g, 'b', False)
    _serve(g, 'a')
    _sync(g, admin, 'b')
    with g.at('b') as ha:
        assert ha.serve_users() is False


def test_serving_needs_the_live_view_and_forwarding(fwd, seed):
    g = fwd
    _built(g, seed, 'b')
    _serve(g, 'b')
    with g.at('b') as ha:
        ha.set_live_view(False)
        assert (ha.serving(), ha.consoles_here()) == (False, False)
        ha.set_live_view(True)
        ha.set_forward_writes(False)
        assert (ha.serving(), ha.consoles_here()) == (False, False)
        ha.set_forward_writes(True)
        assert (ha.serving(), ha.consoles_here()) == (True, True)
    # an instance that acts opens consoles anyway; it is not a serving standby
    for n in 'ae':
        _serve(g, n)
        with g.at(n) as ha:
            assert ha.role() == ('active' if n == 'a' else 'standalone')
            assert (ha.serving(), ha.consoles_here()) == (False, True)


def test_the_settings_route_takes_the_switch_and_audits_it(fwd, seed):
    g = fwd
    admin = _built(g, seed, 'b')
    restarts = list(g.restarts)
    with g.at('b'):
        st = admin.get('/api/ha/status').get_json()
        assert (st['serve_users'], st['serving']) == (False, False)
        assert admin.put('/api/ha/settings', json={'serve_users': 'yes'}).status_code == 400
        r = admin.put('/api/ha/settings', json={'serve_users': True})
        assert r.status_code == 200, r.data
        assert r.get_json() == {'success': True, 'serve_users': True, 'serving': True}
        st = admin.get('/api/ha/status').get_json()
        assert (st['serve_users'], st['serving']) == (True, True)
        # forwarding off: the switch stays, the effect goes
        r = admin.put('/api/ha/settings', json={'serve_users': True, 'forward_writes': False})
        assert r.get_json()['serving'] is False
        st = admin.get('/api/ha/status').get_json()
        assert (st['serve_users'], st['serving']) == (True, False)
    # no restart: it counts from the next request
    assert g.restarts == restarts
    rows = [row['details'] for row in _audit('ha.settings_changed') if 'serving users' in row['details']]
    assert rows == ['serving users as a standby on']


# --- what the others see ---------------------------------------------------------------------

def test_the_members_see_who_serves(fwd, seed, monkeypatch):
    g = fwd
    admin = _built(g, seed, 'bc')
    _watch(g, 'a')
    ms = _members(g, admin, 'a')
    assert ms[IDS['b']]['serving_seen'] is False and ms[IDS['c']]['serving_seen'] is False

    _serve(g, 'b')
    r = _send(g, 'b', g.signed('a', 'b'))
    assert r.status_code == 200 and r.get_json()['serving'] is True
    r = _send(g, 'a', g.signed('b', 'a'))
    assert r.get_json()['serving'] is False
    # the check before a removal notes it too
    with g.at('a') as ha:
        ha.refresh_member(IDS['b'])
        assert ha.member(IDS['b'])['serving_seen'] is True
    _watch(g, 'a')
    _watch(g, 'c')
    ms = _members(g, admin, 'a')
    assert ms[IDS['b']]['serving_seen'] is True and ms[IDS['c']]['serving_seen'] is False
    ms = _members(g, admin, 'c')
    assert ms[IDS['b']]['serving_seen'] is True and ms[IDS['a']]['serving_seen'] is False

    # anything but true is not serving
    from pegaprox.core import ha as ha_mod
    monkeypatch.setattr(ha_mod, 'serving', lambda: 'yes')
    _watch(g, 'a')
    assert _members(g, admin, 'a')[IDS['b']]['serving_seen'] is False
    monkeypatch.undo()
    _serve(g, 'b', False)
    _watch(g, 'a')
    assert _members(g, admin, 'a')[IDS['b']]['serving_seen'] is False


def test_the_banner_of_a_serving_standby(fwd, seed, db, tmp_path, monkeypatch):
    g = fwd
    admin = _built(g, seed, 'b')
    creds = _local_user(db, tmp_path, monkeypatch)
    with g.at('b'):
        banner = admin.get('/api/auth/check').get_json()['ha']
    assert (banner['serving'], banner['leader_reachable'], banner['forwarding']) == (False, True, True)

    _serve(g, 'b')
    with g.at('b'):
        r = g.api.anon().post('/api/auth/login', json=creds)
        assert r.status_code == 200, r.data
        assert r.get_json()['ha']['serving'] is True
        assert r.get_json()['ha']['leader_reachable'] is True
        assert admin.get('/api/auth/check').get_json()['ha']['serving'] is True

    # the leader goes silent: b still serves (consoles here), and says the leader is gone
    g.down.add('a')
    _watch(g, 'b')
    with g.at('b'):
        banner = admin.get('/api/auth/check').get_json()['ha']
    assert (banner['serving'], banner['leader_reachable'], banner['forwarding']) == (True, False, False)
    g.down.discard('a')
    _watch(g, 'b')
    with g.at('b'):
        assert admin.get('/api/auth/check').get_json()['ha']['leader_reachable'] is True
    # an instance that acts says its role and nothing more
    with g.at('a'):
        assert admin.get('/api/auth/check').get_json()['ha'] == {'role': 'active'}


def test_a_member_removed_from_the_group_serves_nobody(fwd, seed, monkeypatch):
    """Its accounts and rights stay those of its last sync, and nothing the leader
    changes reaches it any more: no consoles there, and it does not call itself active.
    The switch stays as the admin left it."""
    g = fwd
    admin = _built(g, seed, 'b')
    mgr = _fake_manager(g.api)
    _registry(monkeypatch, **{CID: mgr})
    mgr.get_vnc_ticket.return_value = {'success': True, 'ticket': 'PVEVNC:x', 'port': '5900'}
    _serve(g, 'b')
    # counterproof: until the removal it serves
    with g.at('b'):
        assert admin.get(f'{VM}/console').status_code == 200
        assert admin.post('/api/ws/token', json={}).status_code == 200

    with g.at('a'):
        r = _post(admin, f"/api/ha/members/{IDS['b']}/remove",
                  {'confirm': 'REMOVE', 'user_password': ADMIN_PW})
    assert r.status_code == 200 and r.get_json()['told'] is True, r.data
    assert g.state('b')['removed'] and g.state('b')['role'] == 'standby'
    g.calls.clear()
    with g.at('b') as ha:
        assert ha.serve_users() is True
        assert (ha.serving(), ha.consoles_here(), ha.leader_reachable()) == (False, False, False)
        r = admin.get(f'{VM}/console')
        assert r.status_code == 409 and r.get_json() == STANDBY_ANSWER
        r = admin.post('/api/ws/token', json={})
        assert r.status_code == 409 and r.get_json()['code'] == 'HA_STANDBY'
        banner = admin.get('/api/auth/check').get_json()['ha']
        st = admin.get('/api/ha/status').get_json()
    assert (banner['serving'], banner['removed'], banner['peer_url']) == (False, True, '')
    assert st['serving'] is False and st['removed']['by'] == IDS['a']
    assert mgr.get_vnc_ticket.call_count == 1 and _forward_calls(g) == []


def test_a_standby_that_lost_its_source_serves_nobody(fwd, seed):
    """The leader unpaired while this one could not be told: nobody to follow."""
    g = fwd
    _built(g, seed, 'b')
    _serve(g, 'b')
    with g.at('b') as ha:
        assert ha.serving() is True
        ha._update(source='f' * 32)
        assert ha.source_id() is None
        assert (ha.serving(), ha.consoles_here()) == (False, False)
        assert ha.banner()['removed'] is False


# --- consoles: the GET routes ------------------------------------------------------------------

def test_the_console_routes_open_on_a_serving_standby(fwd, seed, monkeypatch):
    g = fwd
    admin = _built(g, seed, 'b')
    mgr = _fake_manager(g.api)
    reg = _registry(monkeypatch, **{CID: mgr})
    calls = _console_calls(monkeypatch)
    for method, ret in calls.values():
        if method:
            getattr(mgr, method).return_value = ret
    g.calls.clear()
    with g.at('b'):
        for path in calls:
            r = admin.get(path)
            assert r.status_code == 409 and r.get_json() == STANDBY_ANSWER, path
    assert reg.asked == []

    _serve(g, 'b')
    with g.at('b'):
        for path in calls:
            reg.asked.clear()
            r = admin.get(path)
            assert r.status_code == 200, (path, r.status_code, r.data)
            assert CID in reg.asked, path
    assert mgr.get_vnc_ticket.call_count == 2 and mgr.get_spice_ticket.call_count == 1
    assert _forward_calls(g) == []


def test_an_api_token_gets_no_console_on_a_serving_standby(fwd, seed, monkeypatch):
    """Scripts use the leader: a token keeps the standby answer, a browser session opens."""
    g = fwd
    _built(g, seed, 'b')
    mgr = _fake_manager(g.api)
    _registry(monkeypatch, **{CID: mgr})
    mgr.get_vnc_ticket.return_value = {'success': True, 'ticket': 'PVEVNC:x', 'port': '5900'}
    token = _api_token()
    _serve(g, 'b')
    g.calls.clear()
    with g.at('b'):
        r = g.api.anon().get(f'{VM}/console', headers=token)
        assert r.status_code == 409 and r.get_json() == STANDBY_ANSWER
        r = g.api.anon().post('/api/ws/token', json={}, headers=token)
        assert r.status_code == 409 and r.get_json()['code'] == 'HA_STANDBY'
    assert _forward_calls(g) == [] and mgr.get_vnc_ticket.call_count == 0
    # counterproof: the same token on the leader
    with g.at('a'):
        assert g.api.anon().get(f'{VM}/console', headers=token).status_code == 200
        assert g.api.anon().post('/api/ws/token', json={}, headers=token).status_code == 200


# --- consoles: the POST routes the block in app.py decides ------------------------------------

# what each route answers here once it runs: no such cluster or ESXi server, or a token
LOCAL_ANSWER = {
    '/api/ws/token': 200,
    '/api/clusters/<cluster_id>/nodes/<node>/shell': 404,
    '/api/clusters/<cluster_id>/vms/<node>/<vm_type>/<int:vmid>/termproxy': 404,
    '/api/clusters/<cluster_id>/vms/<node>/<vm_type>/<int:vmid>/vnc-poll': 404,
    '/api/vmware/<vmware_id>/vms/<vm_id>/console': 404,
}


def test_the_console_posts_run_here_on_a_serving_standby(fwd, seed, monkeypatch):
    from pegaprox.globals import ws_tokens
    g = fwd
    admin = _built(g, seed, 'b')
    reg = _registry(monkeypatch)
    entries = sorted(_hook_lists(g.api.app)['_STANDBY_CONSOLES'])
    assert sorted(rule for _m, rule in entries) == sorted(LOCAL_ANSWER)
    before = set(ws_tokens)
    g.calls.clear()
    with g.at('b'):
        for method, rule in entries:
            r = getattr(admin, method.lower())(_concrete(rule), json={})
            assert r.status_code == 409 and r.get_json()['code'] == 'HA_STANDBY', (rule, r.data)
    assert reg.asked == [] and set(ws_tokens) == before and _forward_calls(g) == []

    _serve(g, 'b')
    with g.at('b'):
        for method, rule in entries:
            reg.asked.clear()
            r = getattr(admin, method.lower())(_concrete(rule), json={})
            assert r.status_code == LOCAL_ANSWER[rule], (rule, r.status_code, r.data)
            if rule.startswith('/api/clusters/'):
                assert r.get_json()['error'] == 'Cluster not found' and reg.asked, rule
            elif rule.startswith('/api/vmware/'):
                assert r.get_json()['error'] == 'VMware server not found'
            else:
                assert r.get_json()['token'] in ws_tokens
    assert _forward_calls(g) == []


def test_a_plugin_console_stays_on_the_leader_even_from_a_serving_standby(fwd, seed, probe, monkeypatch):
    """The plugin answers from what this process loaded, and one switched off on the
    leader stays loaded on a member. Its UI works on the leader only anyway."""
    import pegaprox.api.plugins as plugins
    g = fwd
    admin = _built(g, seed, 'b')
    monkeypatch.setitem(plugins._plugin_routes, 'probe', dict(plugins._plugin_routes['probe'],
                                                               **{'vm/console': lambda: {'ticket': 'x'}}))
    for serving in (True, False):
        _serve(g, 'b', serving)
        g.calls.clear()
        with g.at('b'):
            for call in (admin.post, admin.get):
                r = call('/api/plugins/probe/api/vm/console', **({'json': {}} if call == admin.post else {}))
                assert r.status_code == 409 and r.get_json()['code'] == 'HA_STANDBY', (serving, r.data)
        assert _forward_calls(g) == []
    # counterproof: the core console token opens on the serving standby, the plugin
    # console on the leader, and the plugin's other writes still go to the leader
    _serve(g, 'b')
    with g.at('b'):
        assert admin.post('/api/ws/token', json={}).status_code == 200
        assert admin.post('/api/plugins/probe/api/record', json={}).status_code == 200
    assert len(_forward_calls(g)) == 1 and probe[-1]['role'] == 'active'
    with g.at('a'):
        r = admin.post('/api/plugins/probe/api/vm/console', json={})
    assert r.status_code == 200 and r.get_json() == {'ticket': 'x'}, r.data


# --- consoles: the WebSockets ------------------------------------------------------------------

@pytest.mark.parametrize('auth', ['token', 'session'])
def test_the_vnc_sockets_open_on_a_serving_standby(fwd, seed, monkeypatch, auth):
    g = fwd
    admin = _built(g, seed, 'b')
    reg = _registry(monkeypatch)
    app = g.api.app
    main_port = _sock_handler(app, VNC_SOCKET)
    own_port = _standalone_vnc_handler(reg)
    path = f'{VM}/vncwebsocket'

    def on_main_port():
        ws = _SyncWS()
        with app.test_request_context(f'{path}?{_auth_query(auth, admin)}'):
            main_port(ws, CID, 'n1', 'qemu', 100)
        return ws

    def on_own_port():
        ws = _AsyncWS(f'{path}?{_auth_query(auth, admin)}')
        asyncio.run(own_port(ws))
        return ws

    with g.at('b'):
        assert on_main_port().sent == [STANDBY_ANSWER['error']]
        assert on_own_port().closed == (1008, STANDBY_ANSWER['error'])
    assert reg.asked == []

    _serve(g, 'b')
    with g.at('b'):
        ws = on_main_port()
        assert STANDBY_ANSWER['error'] not in ws.sent and reg.asked[-1] == CID
        reg.asked.clear()
        ws = on_own_port()
        assert ws.closed == (1002, 'Cluster not found') and reg.asked[-1] == CID


def test_the_gevent_vnc_socket_and_the_legacy_shell_open_on_a_serving_standby(fwd, seed, monkeypatch):
    g = fwd
    admin = _built(g, seed, 'b')
    reg = _registry(monkeypatch)
    app = g.api.app
    client = app.test_client()
    url = f'{VM}/vncwebsocket?session={admin.session_id}'
    shell = _sock_handler(app, '/api/clusters/<cluster_id>/nodes/<node>/shellws')
    shell_url = f'/api/clusters/{CID}/nodes/n1/shellws?session={admin.session_id}'

    def gevent_vnc():
        ws = _SyncWS()
        client.get(url, base_url='http://localhost', environ_base={'wsgi.websocket': ws})
        return ws

    def legacy_shell():
        ws = _SyncWS()
        with app.test_request_context(shell_url):
            shell(ws, CID, 'n1')
        return [json.loads(m)['message'] for m in ws.sent]

    with g.at('b'):
        assert gevent_vnc().closed == ((1008, STANDBY_ANSWER['error']), {})
        assert legacy_shell() == [STANDBY_ANSWER['error']]
    assert reg.asked == []

    pytest.importorskip('paramiko')
    _serve(g, 'b')
    with g.at('b'):
        assert gevent_vnc().closed is None and reg.asked[-1] == CID
        reg.asked.clear()
        assert legacy_shell() == ['Cluster not found'] and reg.asked[-1] == CID


def test_the_ssh_servers_calls_answer_on_a_serving_standby(fwd, seed, monkeypatch):
    """The ws token check and the cluster-creds of the SSH server process, and what it
    learns there: a standby holds the leader's known_hosts."""
    from pegaprox.utils.realtime import create_ws_token
    g = fwd
    admin = _built(g, seed, 'b')
    reg = _registry(monkeypatch)
    client = g.api.app.test_client()
    client.set_cookie('session', admin.session_id, domain='localhost')

    def validate():
        token = create_ws_token('root', 'admin')
        return client.get(f'/api/ws/token/validate?token={token}&cluster_id={CID}&node=n1&shell=node',
                          base_url='http://localhost')

    def creds():
        return client.get(f'/api/internal/cluster-creds/{CID}', base_url='http://localhost')

    def session_check():
        return client.get('/api/auth/validate', base_url='http://localhost')

    with g.at('b'):
        assert validate().get_json() == STANDBY_ANSWER
        assert creds().get_json() == STANDBY_ANSWER
    assert reg.asked == []

    _serve(g, 'b')
    with g.at('b'):
        r = validate()
        assert r.status_code == 200 and r.get_json()['valid'] is True, r.data
        assert r.get_json()['known_hosts_only'] is True
        r = creds()
        assert r.status_code == 404 and r.get_json()['error'] == 'Cluster not found'
        assert session_check().get_json()['known_hosts_only'] is True
    assert CID in reg.asked
    with g.at('a'):
        assert validate().get_json()['known_hosts_only'] is False
        assert session_check().get_json()['known_hosts_only'] is False


# --- no host key pinned on a standby -----------------------------------------------------------

class _HostKeys(dict):
    def add(self, host, keytype, key):
        self.setdefault(host, {})[keytype] = key


def _fake_paramiko(known):
    """What the SSH server script uses of paramiko: a client whose connect meets the
    host key as `known` or not, and notes whether the keys were saved."""
    fake = types.SimpleNamespace(saved=[])

    class SSHException(Exception):
        pass

    class SSHClient:
        def __init__(self):
            self._host_keys, self._policy = _HostKeys(), None

        def load_host_keys(self, path):
            pass

        def set_missing_host_key_policy(self, policy):
            self._policy = policy

        def connect(self, host, **kw):
            if not known:
                self._policy.missing_host_key(self, host, types.SimpleNamespace(get_name=lambda: 'ssh-ed25519'))

        def save_host_keys(self, path):
            fake.saved.append(path)

        def invoke_shell(self, **kw):
            raise SSHException('no shell in this test')

        def close(self):
            pass

    fake.SSHException = SSHException
    fake.AuthenticationException = type('AuthenticationException', (SSHException,), {})
    fake.MissingHostKeyPolicy = object
    fake.SSHClient = SSHClient
    return fake


class _TalkingWS(_AsyncWS):
    """A browser that sends the node's credentials when asked for them."""

    def __init__(self, path, *incoming):
        super().__init__(path)
        self.incoming = list(incoming)

    async def recv(self):
        if self.incoming:
            return self.incoming.pop(0)
        raise ConnectionError('nothing more to say')


@pytest.mark.parametrize('known_only, known', [(True, False), (True, True), (False, False)])
def test_the_ssh_server_pins_nothing_on_a_standby(tmp_path, monkeypatch, known_only, known):
    pytest.importorskip('paramiko')
    pytest.importorskip('websockets')
    monkeypatch.setenv('PEGAPROX_SSH_KNOWN_HOSTS', str(tmp_path / 'known_hosts'))
    answer = _Resp(200, {'valid': True, 'known_hosts_only': known_only,
                         'cluster_context': {'host': '192.0.2.10', 'node_ips': {}}})
    ns, _asked = _ssh_server({'/api/ws/token/validate': answer})
    fake = _fake_paramiko(known)
    ns['paramiko'] = fake
    ws = _TalkingWS(f'{SHELL}?token=t', json.dumps({'username': 'root', 'password': 'pw'}))
    asyncio.run(ns['ssh_handler'](ws))
    said = ''.join(m for m in ws.sent if isinstance(m, str))
    if known_only and not known:
        assert f'host key of n1 {NOT_KNOWN}' in said, ws.sent
        assert fake.saved == []
    elif known_only:
        # a key it holds: the shell opens (and fails in this test), and nothing is saved
        assert NOT_KNOWN not in said and 'no shell in this test' in said, ws.sent
        assert fake.saved == []
    else:
        # counterproof: the TOFU pin as before, then the (fake) shell fails
        assert NOT_KNOWN not in said and 'no shell in this test' in said, ws.sent
        assert fake.saved == [str(tmp_path / 'known_hosts')]


@pytest.fixture
def known_hosts(tmp_path, monkeypatch):
    import pegaprox.utils.ssh_security as sec
    path = str(tmp_path / 'known_hosts')
    monkeypatch.setattr(sec, '_KNOWN_HOSTS', path)
    return path


class _Transport:
    """A paramiko Transport after the key exchange, as far as the check reads it."""

    def __init__(self, key):
        self.key = key

    def get_remote_server_key(self):
        return self.key


def _roles(g):
    g.write('a', {'role': 'active', 'epoch': 1, 'instance_id': IDS['a'], 'interval': 30,
                  'pairing': None, 'sync': {}})
    g.write('b', {'role': 'standby', 'epoch': 1, 'instance_id': IDS['b'], 'interval': 30,
                  'pairing': None, 'sync': {}, 'serve_users': True})


def test_a_standby_pins_no_host_key_and_takes_the_ones_it_holds(group, known_hosts):
    paramiko = pytest.importorskip('paramiko')
    from pegaprox.utils import ssh_security as sec
    g = group
    _roles(g)
    known, stranger = paramiko.ECDSAKey.generate(), paramiko.ECDSAKey.generate()
    pinned = paramiko.hostkeys.HostKeys()
    pinned.add('192.0.2.10', known.get_name(), known)
    pinned.save(known_hosts)
    before = open(known_hosts).read()

    with g.at('b'):
        assert sec.pins_host_keys_here() is False
        assert sec.cli_hostkey_opts() == ('yes', known_hosts)
        client = sec.secure_ssh_client(paramiko)
        with pytest.raises(paramiko.SSHException, match=f'host key of 192.0.2.11 {NOT_KNOWN}'):
            client._policy.missing_host_key(client, '192.0.2.11', stranger)
        with pytest.raises(paramiko.SSHException, match=f'host key of 192.0.2.11 {NOT_KNOWN}'):
            sec.verify_transport_host_key(_Transport(stranger), '192.0.2.11', paramiko)
        # the key it holds goes through, a changed one is refused as everywhere
        sec.verify_transport_host_key(_Transport(known), '192.0.2.10', paramiko)
        with pytest.raises(paramiko.BadHostKeyException):
            sec.verify_transport_host_key(_Transport(stranger), '192.0.2.10', paramiko)
        # a client carrying a key nobody checked writes nothing either
        client.get_host_keys().add('192.0.2.12', stranger.get_name(), stranger)
        sec.persist_host_keys(client)
    assert open(known_hosts).read() == before

    # counterproof: an instance that acts pins on first sight, as before
    for n in 'ae':
        with open(known_hosts, 'w') as fh:
            fh.write(before)
        with g.at(n):
            assert sec.pins_host_keys_here() is True
            assert sec.cli_hostkey_opts()[0] == 'accept-new'
            client = sec.secure_ssh_client(paramiko)
            client._policy.missing_host_key(client, '192.0.2.11', stranger)
            sec.persist_host_keys(client)
            sec.verify_transport_host_key(_Transport(stranger), '192.0.2.13', paramiko)
        now = paramiko.hostkeys.HostKeys(known_hosts)
        assert now.lookup('192.0.2.11') and now.lookup('192.0.2.13'), n


def test_the_refusal_names_the_address_that_was_tried(group, known_hosts):
    """A standby looks a key up under the address it reached the node at. The leader may
    know the node under another one (another site, another VLAN), which the refusal says:
    a shell on the leader then pins nothing this standby could use."""
    paramiko = pytest.importorskip('paramiko')
    from pegaprox.utils import ssh_security as sec
    g = group
    _roles(g)
    key = paramiko.ECDSAKey.generate()
    # the leader's pin, under the address the leader uses
    pinned = paramiko.hostkeys.HostKeys()
    pinned.add('10.1.0.5', key.get_name(), key)
    pinned.save(known_hosts)
    with g.at('b'):
        sec.verify_transport_host_key(_Transport(key), '10.1.0.5', paramiko)
        for tried, port, shown in (('10.2.0.5', 22, '10.2.0.5'), ('10.2.0.5', 2222, '[10.2.0.5]:2222')):
            with pytest.raises(paramiko.SSHException) as e:
                sec.verify_transport_host_key(_Transport(key), tried, paramiko, port=port)
            said = str(e.value)
            assert said.startswith(f'host key of {shown} is not known here yet'), said
            assert f'if the leader reaches this node at another address than {shown}' in said
        client = sec.secure_ssh_client(paramiko)
        with pytest.raises(paramiko.SSHException, match='another address than 10.2.0.5,'):
            client._policy.missing_host_key(client, '10.2.0.5', key)


def test_the_system_ssh_fallback_is_strict_on_a_standby(group, known_hosts, monkeypatch):
    """utils/ssh.py falls back to sshpass + ssh when paramiko gets nowhere."""
    paramiko = pytest.importorskip('paramiko')
    import socket
    from pegaprox.utils import ssh as sshmod
    g = group
    _roles(g)

    def no_network(*a, **kw):
        raise OSError('no network in this test')
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(paramiko.SSHClient, 'connect', no_network)
    ran = []

    def run(args, **kw):
        ran.append(args)
        return subprocess.CompletedProcess(args, 0, 'ok', '')
    monkeypatch.setattr(subprocess, 'run', run)
    for n, want in (('b', 'yes'), ('a', 'accept-new'), ('e', 'accept-new')):
        with g.at(n):
            rc, out, _err = sshmod._ssh_exec('192.0.2.10', 'root', 'pw', 'true')
        assert (rc, out) == (0, 'ok'), n
        assert f'StrictHostKeyChecking={want}' in ran[-1], (n, ran[-1])
        assert f'UserKnownHostsFile={known_hosts}' in ran[-1]


# --- what only the leader's tables hold ----------------------------------------------------------

LEADER_VIEWS = {
    '/api/clusters/<cluster_id>/active-alerts': f'/api/clusters/{CID}/active-alerts',
    '/api/clusters/<cluster_id>/drift/status': f'/api/clusters/{CID}/drift/status',
    '/api/clusters/<cluster_id>/drift/events': f'/api/clusters/{CID}/drift/events?status=all',
    '/api/push/inbox': '/api/push/inbox?unread=1',
    '/api/migration-history': f'/api/migration-history?cluster_id={CID}',
    '/api/clusters/<cluster_id>/vms/<int:vmid>/migration-history':
        f'/api/clusters/{CID}/vms/100/migration-history',
}
ACKS = {
    ('POST', '/api/drift/events/<int:eid>/acknowledge'): '/api/drift/events/7/acknowledge',
    ('POST', '/api/clusters/<cluster_id>/active-alerts/<fired_id>/ack'):
        f'/api/clusters/{CID}/active-alerts/7/ack',
    ('POST', '/api/push/inbox/clear'): '/api/push/inbox/clear',
}


@pytest.fixture
def recorded(fwd, monkeypatch):
    """swap(rule, method): that route answers where it ran, noting how it was reached."""
    from flask import request
    app = fwd.api.app
    seen = []

    def swap(rule, method):
        endpoints = [r.endpoint for r in app.url_map.iter_rules() if r.rule == rule and method in r.methods]
        assert len(endpoints) == 1, (rule, endpoints)

        def view(*a, **kw):
            from pegaprox.core import ha
            seen.append({'role': ha.role(), 'mark': request.environ.get(ha.FORWARD_ENVIRON),
                         'args': request.args.to_dict()})
            return {'on': ha.role()}
        monkeypatch.setitem(app.view_functions, endpoints[0], view)
    return types.SimpleNamespace(swap=swap, seen=seen)


def test_the_leader_views_are_forwarded_reads(api):
    from pegaprox.core import ha
    assert set(LEADER_VIEWS) == ha.LEADER_ONLY_READS
    assert ha.LEADER_ONLY_READS < ha.FORWARDED_READS


def _failing_forward(g, monkeypatch, how):
    """The forwarded read of b fails the way `how` says; every other call goes through."""
    import pegaprox.api.ha as ha_api
    if how == 'down':
        g.down.add('a')
        return
    if how == 'unreadable':
        monkeypatch.setattr(ha_api, '_forwarded_answer', lambda resp: None)
        return
    plain = g.call

    def call(method, base_url, fingerprint, path, *a, **kw):
        if path == FORWARD:
            if how == 'no_answer':
                raise g.ha.PeerNoAnswer('The peer took the call but sent no answer: ReadTimeout')
            # a proxy in front of the leader while it restarts
            return types.SimpleNamespace(status_code=502, headers={}, content=b'',
                                         json=lambda: {'error': 'Bad Gateway'})
        return plain(method, base_url, fingerprint, path, *a, **kw)
    monkeypatch.setattr(g.ha, '_peer_call', call)


@pytest.mark.parametrize('how', ['no_answer', 'down', 'refused', 'unreadable'])
def test_a_leader_view_is_never_the_standbys_own_rows(fwd, seed, recorded, monkeypatch, how):
    """When the leader does not answer, the list says so (503) instead of showing the
    standby's own rows: those carry ids of its own, and an ack picked from them would be
    forwarded and name another row on the leader."""
    g = fwd
    admin = _built(g, seed, 'b')
    for rule in LEADER_VIEWS:
        recorded.swap(rule, 'GET')
    recorded.swap('/api/vmware/migrations', 'GET')
    _failing_forward(g, monkeypatch, how)
    for rule, path in sorted(LEADER_VIEWS.items()):
        with g.at('b') as ha:
            ha._note_source_heard(IDS['a'], True)
            r = admin.get(path)
        assert r.status_code == 503, (rule, r.status_code, r.data)
        assert r.get_json()['code'] == 'HA_ACTIVE_UNREACHABLE', rule
    # the leader may have run it (its answer got lost on the way), this instance did not
    assert [s for s in recorded.seen if s['role'] != 'active'] == []
    # counterproof: the progress of a job falls back to this instance's own copy
    with g.at('b') as ha:
        ha._note_source_heard(IDS['a'], True)
        r = admin.get('/api/vmware/migrations')
    assert r.status_code == 200 and r.get_json() == {'on': 'standby'}, r.data
    assert [s['role'] for s in recorded.seen if s['role'] != 'active'] == ['standby']


def test_a_leader_view_answers_here_while_nothing_is_forwarded(fwd, seed, recorded):
    """No attempt, no refusal: forwarding switched off, the leader known to be gone, or an
    API token. The route answers as it did before."""
    from pegaprox.utils.auth import create_api_token
    g = fwd
    admin = _built(g, seed, 'b')
    rule = '/api/clusters/<cluster_id>/drift/events'
    recorded.swap(rule, 'GET')
    token = {'Authorization': f"Bearer {create_api_token('root', 'automation', role='admin')['token']}"}
    g.calls.clear()
    with g.at('b') as ha:
        assert g.api.anon().get(LEADER_VIEWS[rule], headers=token).get_json() == {'on': 'standby'}
        ha.set_forward_writes(False)
        assert admin.get(LEADER_VIEWS[rule]).get_json() == {'on': 'standby'}
        ha.set_forward_writes(True)
        ha._note_source_heard(IDS['a'], False)
        assert ha.forwarding() is False
        assert admin.get(LEADER_VIEWS[rule]).get_json() == {'on': 'standby'}
    assert _forward_calls(g) == []


@pytest.mark.parametrize('rule', sorted(LEADER_VIEWS))
def test_a_view_only_the_leader_fills_comes_from_the_leader(fwd, seed, recorded, rule):
    g = fwd
    admin = _built(g, seed, 'b')
    recorded.swap(rule, 'GET')
    path = LEADER_VIEWS[rule]
    g.calls.clear()
    with g.at('b'):
        r = admin.get(path)
    assert r.status_code == 200 and r.get_json() == {'on': 'active'}, r.data
    assert _forward_calls(g) == [('b', 'a', 'POST', FORWARD)]
    assert recorded.seen[-1]['mark']['via'] == URLS['b']
    assert recorded.seen[-1]['args'] == dict(parse_qsl(urlsplit(path).query))
    # a read changes nothing: no sync after it
    assert g.pulls == []

    # forwarding off: this instance's own answer, and nothing sent
    g.calls.clear()
    with g.at('b') as ha:
        ha.set_forward_writes(False)
        r = admin.get(path)
    assert r.status_code == 200 and r.get_json() == {'on': 'standby'}
    assert _forward_calls(g) == []


@pytest.mark.parametrize('method, rule', sorted(ACKS))
def test_an_acknowledgement_goes_to_the_leader(fwd, seed, recorded, method, rule):
    g = fwd
    admin = _built(g, seed, 'b')
    lists = _hook_lists(g.api.app)
    assert (method, rule) not in lists['_STANDBY_NOT_FORWARDED']
    # the two that name rows of this instance's own tables stay kept back
    assert {('DELETE', '/api/auto-install/runs/<run_id>'),
            ('POST', '/api/insights/force-snapshot')} <= lists['_STANDBY_NOT_FORWARDED']
    recorded.swap(rule, method)
    g.calls.clear()
    with g.at('b'):
        r = admin.post(ACKS[(method, rule)], json={})
    assert r.status_code == 200 and r.get_json() == {'on': 'active'}, r.data
    assert _forward_calls(g) == [('b', 'a', 'POST', FORWARD)]
    assert recorded.seen[-1]['mark']['via'] == URLS['b']
    assert g.pulls == ['b']

    # forwarding off: refused, as every change is
    g.calls.clear()
    with g.at('b') as ha:
        ha.set_forward_writes(False)
        r = admin.post(ACKS[(method, rule)], json={})
    assert r.status_code == 409 and r.get_json()['code'] == 'HA_STANDBY'
    assert _forward_calls(g) == [] and len(recorded.seen) == 1


def test_the_inbox_clear_runs_on_the_leader_as_the_user(fwd, seed):
    """The real route, not a stand-in: the leader marks the user's rows read."""
    from datetime import datetime
    from pegaprox.api.push import _ensure_inbox_table
    from pegaprox.core.db import get_db
    g = fwd
    admin = _built(g, seed, 'b')
    _ensure_inbox_table()
    db = get_db()
    db.conn.cursor().execute(
        "INSERT INTO push_inbox (username, title, body, severity, url, tag, created_at) "
        "VALUES ('root', 't', 'b', 'info', '/', 'x', ?)", (datetime.now().isoformat(),))
    db.conn.commit()
    g.calls.clear()
    with g.at('b'):
        r = admin.post('/api/push/inbox/clear', json={})
        assert r.status_code == 200 and r.get_json() == {'ok': True}, r.data
        items = admin.get('/api/push/inbox').get_json()['items']
    assert _forward_calls(g) == [('b', 'a', 'POST', FORWARD)] * 2
    assert items and all(i['read_at'] for i in items)
