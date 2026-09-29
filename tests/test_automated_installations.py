"""Automated installations - PegaProx as the answer-file server for the Proxmox
auto-installer.

An operator stores an answer file, PegaProx hands out a fetch token once, the
prepared ISO POSTs to /api/auto-install/answer with it and gets the file back. The
served file carries a webhook with a callback token of its own, and what the
machine reports there closes its run.

Everything drives the real Flask app. The installer half goes through a RAW test
client: the harness client adds the same-origin and XHR headers a real installer
cannot send, and with those on this would pass even if the CSRF gate turned every
installer away. The payloads are the ones documented on the Proxmox wiki
(Automated_Installation), trimmed.
"""
import copy
import json
import tomllib
from urllib.parse import urlsplit

import pytest


ANSWER = """\
[global]
keyboard = "de"
country = "de"
fqdn = "pve01.lab.example.com"
mailto = "root@example.com"
timezone = "Europe/Berlin"
root-password = "hunter2-in-the-rack"

[network]
source = "from-dhcp"

[disk-setup]
filesystem = "ext4"
disk-list = ["sda"]
"""

# what the installer POSTs to the fetch URL (wiki, schema 1.0)
FETCH_BODY = {
    '$schema': {'version': '1.0'},
    'product': {'fullname': 'Proxmox VE', 'product': 'pve', 'enable_btrfs': True},
    'iso': {'release': '9.0', 'isorelease': '1'},
    'dmi': {
        'system': {'serial': 'JH7K2X3', 'name': 'PowerEdge R660', 'sku': 'SKU',
                   'uuid': '6fb36fd4-77a1-57e6-13ae-000000cb48d7'},
        'baseboard': {'name': 'Z13PP-D32 Series', 'serial': '23041870000'},
        'chassis': {'asset_tag': 'To be filled by O.E.M.', 'serial': 'I02308'},
    },
    'network_interfaces': [{'link': 'enp6s18', 'mac': 'aa:bb:cc:dd:ee:01'},
                           {'link': 'enp6s19', 'mac': 'aa:bb:cc:dd:ee:02'}],
}

# what the installer POSTs to the webhook after a successful install (wiki, schema 1.2)
WEBHOOK_BODY = {
    '$schema': {'version': '1.2'},
    'debian-version': '13.1',
    'product': {'fullname': 'Proxmox VE', 'short': 'pve', 'version': '9.0.3'},
    'iso': {'release': '9.0', 'isorelease': '1'},
    'dmi': {'system': {'serial': 'JH7K2X3', 'uuid': '6fb36fd4-77a1-57e6-13ae-000000cb48d7',
                       'name': 'PowerEdge R660'}},
    'filesystem': 'ext4',
    'fqdn': 'pve01.lab.example.com',
    'machine-id': 'b8737afea804482697ffe04db69c73d1',
    'network-interfaces': [{'name': 'lan', 'mac': 'aa:bb:cc:dd:ee:01', 'is-management': True}],
    'ssh-public-host-keys': {'ed25519': 'ssh-ed25519 AAAA root@pve01.lab.example.com'},
}


def _admin(api, seed):
    return api.as_user(seed.user('root', role='admin'))


def _create(client, **over):
    payload = {'name': 'rack-7 node', 'description': 'lab', 'answer': ANSWER}
    payload.update(over)
    r = client.post('/api/auto-install/profiles', json=payload)
    assert r.status_code == 201, r.data
    return r.get_json()


def _raw(api):
    return api.app.test_client()


def _fetch(api, token, info=None, via='header', **kw):
    """The installer's fetch. via='header' is --answer-auth-token, 'query' the
    fallback for ISOs without it."""
    body = FETCH_BODY if info is None else info
    if via == 'header':
        headers = dict(kw.pop('headers', {}) or {})
        headers['Authorization'] = f'Bearer pegaprox:{token}'
        return _raw(api).post('/api/auto-install/answer', json=body,
                              base_url='http://localhost', headers=headers, **kw)
    return _raw(api).post(f'/api/auto-install/answer?token={token}', json=body,
                          base_url='http://localhost', **kw)


def _hook(served):
    return tomllib.loads(served)['post-installation-webhook']


def _report(api, served, body=None):
    """POST to the webhook URL written into a served file, as the installer does."""
    parts = urlsplit(_hook(served)['url'])
    return _raw(api).post(f'{parts.path}?{parts.query}', json=body if body is not None else WEBHOOK_BODY,
                          base_url='http://localhost')


def _runs(client):
    return client.get('/api/auto-install/runs').get_json()


def _profile_row(profile_id):
    from pegaprox.core.db import get_db
    return get_db().conn.cursor().execute(
        'SELECT * FROM auto_install_profiles WHERE id = ?', (profile_id,)).fetchone()


# --- the fetch token ----------------------------------------------------------

def test_the_token_is_handed_out_once_and_never_again(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    token = created['token']
    assert token.startswith('pgxai_')

    again = c.get(f"/api/auto-install/profiles/{created['id']}").get_json()
    listed = c.get('/api/auto-install/profiles').get_json()
    for p in [again] + listed['profiles']:
        assert 'token' not in p
        assert 'token_hash' not in p, 'the hash is not the client\'s business either'
    assert listed['can_manage'] is True
    assert len(again.get('token_hint') or '') < len(token)


def test_the_token_is_not_stored_in_the_clear(api, seed):
    import hashlib
    created = _create(_admin(api, seed))
    row = _profile_row(created['id'])
    assert created['token'] not in row['token_hash']
    assert row['token_hash'] == hashlib.sha256(created['token'].encode()).hexdigest()


def test_rotating_the_token_stops_the_old_one(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    old = created['token']
    assert _fetch(api, old).status_code == 200
    new = c.post(f"/api/auto-install/profiles/{created['id']}/token").get_json()['token']
    assert new != old
    assert _fetch(api, old).status_code == 403
    assert _fetch(api, new).status_code == 200


@pytest.mark.parametrize('via', ['header', 'query'])
def test_the_installer_gets_the_answer_file_without_a_session(api, seed, via):
    """No session, no Origin, no X-Requested-With - the way an ISO arrives, both
    with --answer-auth-token and with the token in the URL."""
    token = _create(_admin(api, seed))['token']
    r = _fetch(api, token, via=via)
    assert r.status_code == 200, r.data
    assert r.headers['Content-Type'] == 'text/plain; charset=utf-8', r.headers['Content-Type']
    body = r.data.decode()
    assert 'pve01.lab.example.com' in body
    assert body.count('[global]') == 1


def test_the_auth_token_name_part_is_ignored(api, seed):
    """--answer-auth-token is name:secret; the name is the operator's label."""
    token = _create(_admin(api, seed))['token']
    r = _raw(api).post('/api/auto-install/answer', json=FETCH_BODY, base_url='http://localhost',
                       headers={'Authorization': f'Bearer rack7-node-a:{token}'})
    assert r.status_code == 200, r.data


def test_a_bad_or_missing_token_is_refused_and_no_run_is_recorded(api, seed):
    c = _admin(api, seed)
    _create(c)
    assert _fetch(api, 'pgxai_not-a-real-token').status_code == 403
    r = _raw(api).post('/api/auto-install/answer', json=FETCH_BODY, base_url='http://localhost')
    assert r.status_code == 403
    assert b'pve01' not in r.data
    assert _runs(c) == []


@pytest.mark.parametrize('field,value', [
    ('enabled', False),
    ('expires_at', '2001-01-01T00:00:00Z'),
])
def test_a_profile_that_is_off_or_expired_serves_nothing(api, seed, field, value):
    c = _admin(api, seed)
    created = _create(c)
    assert c.put(f"/api/auto-install/profiles/{created['id']}", json={field: value}).status_code == 200
    assert _fetch(api, created['token']).status_code == 403


def test_the_use_limit_is_enforced(api, seed):
    token = _create(_admin(api, seed), max_uses=1)['token']
    assert _fetch(api, token).status_code == 200
    assert _fetch(api, token).status_code == 403


# --- the claim: decided after the body was read, not before --------------------

def _between_check_and_claim(monkeypatch, action):
    """Run `action` after the usability pre-check and the body read, right before
    the use is claimed - the window a slowly sent body holds open under gevent."""
    import pegaprox.api.auto_install as ai
    real = ai._summarise_system

    def hooked(info):
        action()
        return real(info)
    monkeypatch.setattr(ai, '_summarise_system', hooked)


def test_two_fetches_cannot_both_take_the_last_use(api, seed, monkeypatch):
    c = _admin(api, seed)
    created = _create(c, max_uses=1)
    from pegaprox.core.db import get_db

    def the_other_fetch_won():
        get_db().conn.execute('UPDATE auto_install_profiles SET uses = 1 WHERE id = ?', (created['id'],))
        get_db().conn.commit()
    _between_check_and_claim(monkeypatch, the_other_fetch_won)

    r = _fetch(api, created['token'])
    assert r.status_code == 403, r.data
    assert b'hunter2' not in r.data
    assert _profile_row(created['id'])['uses'] == 1
    assert _runs(c) == []


@pytest.mark.parametrize('revoke', [
    "UPDATE auto_install_profiles SET enabled = 0 WHERE id = ?",
    "UPDATE auto_install_profiles SET token_hash = 'rotated' WHERE id = ?",
    "UPDATE auto_install_profiles SET expires_at = '2001-01-01T00:00:00+00:00' WHERE id = ?",
])
def test_a_revoke_during_the_fetch_stops_it(api, seed, monkeypatch, revoke):
    c = _admin(api, seed)
    created = _create(c)
    from pegaprox.core.db import get_db

    def revoked():
        get_db().conn.execute(revoke, (created['id'],))
        get_db().conn.commit()
    _between_check_and_claim(monkeypatch, revoked)

    r = _fetch(api, created['token'])
    assert r.status_code == 403, r.data
    assert b'hunter2' not in r.data


# --- the served file --------------------------------------------------------------

def test_the_served_file_calls_back_with_a_run_token_not_the_fetch_token(api, seed):
    """The fetch token opens the answer file. Writing it into that file would put
    it on every installed host, where it keeps fetching the root password."""
    token = _create(_admin(api, seed))['token']
    served = _fetch(api, token).data.decode()
    hook = _hook(served)
    assert hook['url'].startswith('http://localhost/api/auto-install/progress?token=')
    assert token not in served
    assert 'auth-token' not in hook, 'older installers refuse unknown keys in this table'


def test_an_answer_file_with_its_own_webhook_section_is_replaced_not_duplicated(api, seed):
    own = ANSWER + '\n[post-installation-webhook]\nurl = "https://elsewhere.invalid/hook"\n'
    served = _fetch(api, _create(_admin(api, seed), answer=own)['token']).data.decode()
    assert served.count('[post-installation-webhook]') == 1
    assert 'elsewhere.invalid' not in _hook(served)['url']


def test_a_section_after_the_webhook_survives_the_strip(api, seed):
    own = (ANSWER + '\n[post-installation-webhook]\nurl = "https://elsewhere.invalid/hook"\n'
           + '\n[first-boot]\nsource = "from-url"\nurl = "https://example.com/fb"\n')
    served = _fetch(api, _create(_admin(api, seed), answer=own)['token']).data.decode()
    assert tomllib.loads(served)['first-boot']['url'] == 'https://example.com/fb'


def test_a_dotted_webhook_cannot_be_saved(api, seed):
    """We can only replace the table form; a dotted one would end up defined twice
    in the served file and every fetch would 500."""
    # top-level dotted key: it has to come before the first table header
    own = 'post-installation-webhook.url = "https://x.invalid"\n\n' + ANSWER
    r = _admin(api, seed).post('/api/auto-install/profiles', json={'name': 'x', 'answer': own})
    assert r.status_code == 400, r.data


def test_an_explicit_callback_url_wins_and_keeps_its_own_query(api, seed):
    created = _create(_admin(api, seed),
                      callback_url='https://pegaprox.example.com/api/auto-install/progress?via=dmz')
    url = _hook(_fetch(api, created['token']).data.decode())['url']
    assert url.startswith('https://pegaprox.example.com/api/auto-install/progress?')
    assert 'via=dmz' in url and '&token=' in url, url


def test_a_fingerprint_has_to_look_like_one(api, seed):
    r = _admin(api, seed).post('/api/auto-install/profiles',
                               json={'name': 'x', 'answer': ANSWER, 'callback_fingerprint': 'AB:CD'})
    assert r.status_code == 400
    assert 'fingerprint' in r.get_json()['error']


@pytest.mark.parametrize('host', ['evil.example.com"', 'host with spaces'])
def test_a_hostile_host_header_does_not_reach_the_answer_file(api, seed, host):
    """The host lands in a TOML string. An unrecognised one costs the callback
    section, not the install. (werkzeug already answers a Host with a path in it
    with its own 400, so only these two reach the handler.)"""
    token = _create(_admin(api, seed))['token']
    r = _fetch(api, token, headers={'Host': host})
    assert r.status_code == 200, r.data
    parsed = tomllib.loads(r.data.decode())
    assert parsed['global']['root-password'] == 'hunter2-in-the-rack'
    assert 'post-installation-webhook' not in parsed


def test_forwarded_headers_count_only_from_a_trusted_proxy(api, seed, monkeypatch):
    import pegaprox.utils.audit as audit
    token = _create(_admin(api, seed))['token']
    headers = {'X-Forwarded-Host': 'attacker.example.net', 'X-Forwarded-Proto': 'https'}

    monkeypatch.setattr(audit, '_is_trusted_proxy', lambda addr: False)
    url = _hook(_fetch(api, token, headers=dict(headers)).data.decode())['url']
    assert url.startswith('http://localhost/'), url

    monkeypatch.setattr(audit, '_is_trusted_proxy', lambda addr: True)
    hook = _hook(_fetch(api, token, headers=dict(headers)).data.decode())
    assert hook['url'].startswith('https://attacker.example.net/')
    assert 'cert-fingerprint' not in hook, 'behind a proxy the certificate on the wire is not ours'


def test_an_undecryptable_answer_is_a_500_and_costs_no_use(api, seed):
    c = _admin(api, seed)
    created = _create(c, max_uses=1)
    from pegaprox.core.db import get_db
    get_db().conn.execute("UPDATE auto_install_profiles SET answer_encrypted = 'aes256:garbage' WHERE id = ?",
                          (created['id'],))
    get_db().conn.commit()
    r = _fetch(api, created['token'])
    assert r.status_code == 500
    assert _profile_row(created['id'])['uses'] == 0
    assert _runs(c) == []


# --- runs and the webhook ----------------------------------------------------------

def test_a_fetch_shows_up_as_an_installing_machine(api, seed):
    c = _admin(api, seed)
    _fetch(api, _create(c)['token'])
    runs = _runs(c)
    assert len(runs) == 1
    run = runs[0]
    assert run['status'] == 'installing'
    assert run['product'] == 'PowerEdge R660'
    assert run['version'] == '9.0'
    assert run['fingerprint'] == 'serial:JH7K2X3'
    assert run['profile_name'] == 'rack-7 node'
    assert 'callback_token_hash' not in run


def test_the_same_machine_retrying_does_not_become_two_machines(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    _fetch(api, token)
    _fetch(api, token)
    assert len(_runs(c)) == 1


def test_two_machines_on_one_profile_are_two_runs(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    _fetch(api, token)
    other = copy.deepcopy(FETCH_BODY)
    other['dmi']['system']['serial'] = 'OTHER-9'
    _fetch(api, token, info=other)
    assert len(_runs(c)) == 2


@pytest.mark.parametrize('serial,uuid,expected', [
    ('To be filled by O.E.M.', '6fb36fd4-77a1-57e6-13ae-000000cb48d7',
     'uuid:6fb36fd4-77a1-57e6-13ae-000000cb48d7'),
    ('', '00000000-0000-0000-0000-000000000000', 'mac:aa:bb:cc:dd:ee:01'),
])
def test_a_machine_without_a_usable_serial_is_still_recognised(api, seed, serial, uuid, expected):
    """VMs typically report no serial but an SMBIOS uuid."""
    c = _admin(api, seed)
    info = copy.deepcopy(FETCH_BODY)
    info['dmi']['system']['serial'] = serial
    info['dmi']['system']['uuid'] = uuid
    _fetch(api, _create(c)['token'], info=info)
    assert _runs(c)[0]['fingerprint'] == expected


def test_an_oversized_inventory_is_kept_readable(api, seed):
    c = _admin(api, seed)
    fat = dict(FETCH_BODY)
    fat['dmesg'] = 'x' * 200000
    _fetch(api, _create(c)['token'], info=fat)
    run = _runs(c)[0]
    assert run['system_info'].get('truncated') is True, run['system_info']
    assert 'dmesg' in run['system_info']['keys']
    assert run['product'] == 'PowerEdge R660'


def test_the_installers_own_webhook_marks_the_run_installed(api, seed):
    """The real body has no status field; it is sent only after a successful
    install. Reading it as 'still installing' left every real run open for good."""
    c = _admin(api, seed)
    served = _fetch(api, _create(c)['token']).data.decode()
    r = _report(api, served)
    assert r.status_code == 200, r.data
    run = _runs(c)[0]
    assert run['status'] == 'installed'
    assert run['hostname'] == 'pve01.lab.example.com'
    assert run['version'] == '9.0.3'
    assert run['system_info']['post_install']['machine-id'] == 'b8737afea804482697ffe04db69c73d1'


@pytest.mark.parametrize('payload,expected', [
    ({'status': 'installed'}, 'installed'),
    ({'status': 'ok'}, 'installed'),
    ({'success': True}, 'installed'),
    ({'success': False, 'message': 'disk too small'}, 'failed'),
    ({'status': 'error', 'error': 'no nic'}, 'failed'),
    ({'error': 'boom'}, 'failed'),
    ({'something': 'else'}, 'installing'),
])
def test_an_explicit_status_from_a_script_is_honoured(api, seed, payload, expected):
    c = _admin(api, seed)
    served = _fetch(api, _create(c)['token']).data.decode()
    assert _report(api, served, payload).status_code == 200
    assert _runs(c)[0]['status'] == expected


def test_a_callback_token_closes_its_own_run_and_no_other(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    first = _fetch(api, token).data.decode()
    other = copy.deepcopy(FETCH_BODY)
    other['dmi']['system']['serial'] = 'OTHER-9'
    _fetch(api, token, info=other)

    _report(api, first)
    by_fp = {r['fingerprint']: r['status'] for r in _runs(c)}
    assert by_fp == {'serial:JH7K2X3': 'installed', 'serial:OTHER-9': 'installing'}


def test_a_finished_install_can_still_report_after_the_token_is_spent(api, seed):
    c = _admin(api, seed)
    served = _fetch(api, _create(c, max_uses=1)['token']).data.decode()
    assert _report(api, served).status_code == 200
    assert _runs(c)[0]['status'] == 'installed'


def test_the_fetch_token_does_not_work_as_a_callback(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    _fetch(api, token)
    r = _raw(api).post(f'/api/auto-install/progress?token={token}', json={'status': 'failed'},
                       base_url='http://localhost')
    assert r.status_code == 403
    assert _runs(c)[0]['status'] == 'installing'


def test_progress_from_an_unknown_token_is_refused(api, seed):
    c = _admin(api, seed)
    _fetch(api, _create(c)['token'])
    r = _raw(api).post('/api/auto-install/progress?token=nope', json={'status': 'failed'},
                       base_url='http://localhost')
    assert r.status_code == 403
    assert _runs(c)[0]['status'] == 'installing'


# --- who may do what ------------------------------------------------------------

def test_the_answer_file_is_encrypted_at_rest(api, seed):
    created = _create(_admin(api, seed))
    from pegaprox.core.db import get_db
    stored = _profile_row(created['id'])['answer_encrypted']
    assert 'hunter2-in-the-rack' not in stored
    assert get_db()._decrypt(stored) == ANSWER


def _viewer(api, seed):
    return api.as_user(seed.user('watcher', role='viewer', permissions=['autoinstall.view']))


def test_view_only_sees_the_shape_of_the_file_not_the_password(api, seed):
    created = _create(_admin(api, seed))
    body = _viewer(api, seed).get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert body['answer_redacted'] is True
    assert 'hunter2-in-the-rack' not in body['answer']
    assert 'root-password' in body['answer']
    assert 'pve01.lab.example.com' in body['answer']
    assert _viewer(api, seed).get('/api/auto-install/profiles').get_json()['can_manage'] is False


SECRET = 'S3cr3t-Pa55-9f2c'
SPELLINGS = [
    ('quoted key', '"root-password" = "%s"'),
    ('single-quoted key', "'root-password' = '%s'"),
    ('snake_case alias', 'root_password = "%s"'),
    ('multi-line string', 'root-password = """\n%s"""'),
    ('hashed, inline', 'root-password-hashed = "%s"'),
]


@pytest.mark.parametrize('label,line', SPELLINGS, ids=[s[0] for s in SPELLINGS])
def test_no_spelling_of_the_password_reaches_a_view_only_reader(api, seed, label, line):
    answer = ANSWER.replace('root-password = "hunter2-in-the-rack"', line % SECRET)
    created = _create(_admin(api, seed), answer=answer)
    body = _viewer(api, seed).get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert SECRET not in body['answer'], (label, body['answer'])


@pytest.mark.parametrize('form', [
    'global = { keyboard = "de", country = "de", fqdn = "pve01.lab.example.com", '
    'mailto = "root@example.com", timezone = "Europe/Berlin", root-password = "%s" }\n',
    'global.keyboard = "de"\nglobal.country = "de"\nglobal.fqdn = "pve01.lab.example.com"\n'
    'global.mailto = "root@example.com"\nglobal.timezone = "Europe/Berlin"\n'
    'global.root-password = "%s"\n',
], ids=['inline table', 'dotted keys'])
def test_a_file_that_cannot_be_blanked_is_not_shown_at_all(api, seed, form):
    rest = ANSWER.split('[network]', 1)[1]
    answer = (form % SECRET) + '\n[network]' + rest
    created = _create(_admin(api, seed), answer=answer)
    body = _viewer(api, seed).get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert SECRET not in body['answer']


def test_view_only_cannot_create_or_rotate(api, seed):
    created = _create(_admin(api, seed))
    w = _viewer(api, seed)
    assert w.post('/api/auto-install/profiles', json={'name': 'x', 'answer': ANSWER}).status_code == 403
    assert w.post(f"/api/auto-install/profiles/{created['id']}/token").status_code == 403
    assert w.delete(f"/api/auto-install/profiles/{created['id']}").status_code == 403


def test_a_user_without_either_permission_sees_nothing(api, seed):
    _create(_admin(api, seed))
    nobody = api.as_user(seed.user('nobody', role='user'))
    assert nobody.get('/api/auto-install/profiles').status_code == 403
    assert nobody.get('/api/auto-install/runs').status_code == 403


def test_an_unconfined_operator_with_the_permission_can_manage(api, seed):
    """Not an admin, but sees every cluster and holds autoinstall.manage. A gate
    tested only with admins passes while it refuses every ordinary operator."""
    ops = api.as_user(seed.user('ops', role='user',
                                permissions=['autoinstall.view', 'autoinstall.manage']))
    created = _create(ops)
    assert ops.get('/api/auto-install/profiles').get_json()['can_manage'] is True
    got = ops.get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert got['answer_redacted'] is False
    assert 'hunter2-in-the-rack' in got['answer']
    assert ops.post(f"/api/auto-install/profiles/{created['id']}/token").status_code == 200


def test_a_tenant_confined_holder_is_turned_away_everywhere(api, seed):
    """Profiles belong to no tenant and carry root passwords. A tenant admin who
    holds both permissions must not read or rewrite another tenant's files."""
    created = _create(_admin(api, seed))
    seed.tenant('globex', ['cluster_globex'])
    t = api.as_user(seed.user('globex_admin', role='user', tenant_id='globex',
                              permissions=['autoinstall.view', 'autoinstall.manage']))
    pid = created['id']
    calls = [
        t.get('/api/auto-install/profiles'),
        t.get(f'/api/auto-install/profiles/{pid}'),
        t.put(f'/api/auto-install/profiles/{pid}', json={'name': 'mine now', 'answer': ANSWER}),
        t.post(f'/api/auto-install/profiles/{pid}/token'),
        t.delete(f'/api/auto-install/profiles/{pid}'),
        t.get('/api/auto-install/runs'),
        t.post('/api/auto-install/profiles', json={'name': 'x', 'answer': ANSWER}),
    ]
    for r in calls:
        assert r.status_code == 403, (r.request.path, r.status_code, r.data)
        assert b'tenant' in r.data, r.data         # our gate, not a missing permission
    assert _admin(api, seed).get(f'/api/auto-install/profiles/{pid}').get_json()['name'] == 'rack-7 node'


# --- validation -------------------------------------------------------------------

@pytest.mark.parametrize('answer,needle', [
    ('not toml at all {{{', 'Not valid TOML'),
    ('[global]\nkeyboard = "de"\n', 'country'),
    (ANSWER.replace('[disk-setup]\nfilesystem = "ext4"\ndisk-list = ["sda"]\n', ''), 'disk-setup'),
    (ANSWER.replace('source = "from-dhcp"', 'source = "magic"'), 'from-dhcp'),
    (ANSWER.replace('fqdn = "pve01.lab.example.com"', 'fqdn = "pve01"'), 'fully qualified'),
    (ANSWER.replace('fqdn = "pve01.lab.example.com"', 'fqdn.source = "from-magic"'), 'fqdn.source'),
])
def test_the_validator_catches_what_the_installer_would_refuse(api, seed, answer, needle):
    body = _admin(api, seed).post('/api/auto-install/validate', json={'answer': answer}).get_json()
    assert body['valid'] is False
    assert any(needle in e for e in body['errors']), body['errors']


def test_a_good_answer_file_validates(api, seed):
    body = _admin(api, seed).post('/api/auto-install/validate', json={'answer': ANSWER}).get_json()
    assert body['valid'] is True
    assert any('clear text' in w for w in body['warnings'])


def test_the_dhcp_form_of_fqdn_is_accepted(api, seed):
    """fqdn.source = "from-dhcp" is documented; it used to fail the dot check."""
    answer = ANSWER.replace('fqdn = "pve01.lab.example.com"',
                            'fqdn.source = "from-dhcp"\nfqdn.domain = "lab.example.com"')
    body = _admin(api, seed).post('/api/auto-install/validate', json={'answer': answer}).get_json()
    assert body['valid'] is True, body


def test_the_raid_warning_reads_the_dotted_key(api, seed):
    zfs = ANSWER.replace('filesystem = "ext4"', 'filesystem = "zfs"\nzfs.raid = "raid1"')
    c = _admin(api, seed)
    with_raid = c.post('/api/auto-install/validate', json={'answer': zfs}).get_json()
    without = c.post('/api/auto-install/validate',
                     json={'answer': zfs.replace('zfs.raid = "raid1"\n', '')}).get_json()
    assert not any('raid' in w for w in with_raid['warnings']), with_raid['warnings']
    assert any('raid' in w for w in without['warnings']), without['warnings']


def test_an_invalid_answer_file_cannot_be_saved(api, seed):
    c = _admin(api, seed)
    assert c.post('/api/auto-install/profiles', json={'name': 'broken', 'answer': '[global]\n'}).status_code == 400
    assert c.get('/api/auto-install/profiles').get_json()['profiles'] == []


# --- updates, expiry, lifecycle -----------------------------------------------------

def test_editing_without_resending_the_answer_keeps_it(api, seed):
    """The UI can show a blanked file; a save from that screen must not write the
    blanking back over the real password."""
    c = _admin(api, seed)
    created = _create(c)
    r = c.put(f"/api/auto-install/profiles/{created['id']}", json={'name': 'renamed'})
    assert r.status_code == 200, r.data
    full = c.get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert full['name'] == 'renamed'
    assert 'hunter2-in-the-rack' in full['answer']


def test_a_partial_update_keeps_every_field_it_did_not_send(api, seed):
    """Falling back to the create defaults re-enabled a revoked profile and lifted
    its use limit, expiry and pin on a PUT that only renamed it."""
    c = _admin(api, seed)
    fp = ':'.join(['AB'] * 32)
    created = _create(c, max_uses=1, expires_at='2099-01-01T00:00:00Z', enabled=False,
                      callback_url='https://cb.example.com/p', callback_fingerprint=fp,
                      target_cluster_id='cluster_1', description='keep me')
    assert c.put(f"/api/auto-install/profiles/{created['id']}", json={'name': 'renamed'}).status_code == 200
    got = c.get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert got['enabled'] is False
    assert got['max_uses'] == 1
    assert got['expires_at'] == '2099-01-01T00:00:00+00:00'
    assert got['callback_url'] == 'https://cb.example.com/p'
    assert got['callback_fingerprint'] == fp
    assert got['target_cluster_id'] == 'cluster_1'
    assert got['description'] == 'keep me'
    assert _fetch(api, created['token']).status_code == 403


@pytest.mark.parametrize('sent,stored', [
    ('2099-01-01T12:00:00Z', '2099-01-01T12:00:00+00:00'),
    ('2099-01-01T14:00:00+02:00', '2099-01-01T12:00:00+00:00'),
    ('2099-01-01T12:00', '2099-01-01T12:00:00+00:00'),
    ('2099-01-01', '2099-01-01T00:00:00+00:00'),
])
def test_an_expiry_is_stored_in_utc_and_does_not_break_the_fetch(api, seed, sent, stored):
    """An offset-aware value used to be accepted and then 500 every fetch, because
    it was compared with a naive local time."""
    c = _admin(api, seed)
    created = _create(c, expires_at=sent)
    assert c.get(f"/api/auto-install/profiles/{created['id']}").get_json()['expires_at'] == stored
    assert _fetch(api, created['token']).status_code == 200


def test_an_unreadable_expiry_fails_closed(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    from pegaprox.core.db import get_db
    get_db().conn.execute("UPDATE auto_install_profiles SET expires_at = 'next tuesday' WHERE id = ?",
                          (created['id'],))
    get_db().conn.commit()
    assert _fetch(api, created['token']).status_code == 403


def test_deleting_a_profile_takes_its_runs_with_it(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    served = _fetch(api, created['token']).data.decode()
    assert c.delete(f"/api/auto-install/profiles/{created['id']}").status_code == 200
    assert _runs(c) == []
    assert _fetch(api, created['token']).status_code == 403
    assert _report(api, served).status_code == 403


# --- noise and logs -----------------------------------------------------------------

def test_refusals_are_audited_but_not_flooded(api, seed):
    import pegaprox.api.auto_install as ai
    ai._refusals.clear()
    _create(_admin(api, seed))
    for _ in range(25):
        _fetch(api, 'pgxai_guess')
    from pegaprox.core.db import get_db
    rows = get_db().conn.cursor().execute(
        "SELECT severity FROM audit_log WHERE action = 'autoinstall.fetch_refused'").fetchall()
    assert 1 <= len(rows) <= ai._REFUSAL_AUDIT_MAX
    assert all(r['severity'] != 'critical' for r in rows)


def test_a_token_in_the_query_string_is_blanked_in_the_access_log():
    from pegaprox.utils.sanitization import redact_request_line
    line = '10.0.0.5 - - [ts] "POST /api/auto-install/answer?token=pgxai_live&via=dmz HTTP/1.1" 200 512 0.01'
    out = redact_request_line(line)
    assert 'pgxai_live' not in out
    assert 'via=dmz' in out and '/api/auto-install/answer' in out


def test_the_spec_says_which_token_the_installer_endpoints_take(api):
    from pegaprox.cli.gen_openapi import build
    paths = build(api.app)
    for path in ('/api/auto-install/answer', '/api/auto-install/progress'):
        op = paths[path]['post']
        assert op['security'] == [{'installToken': []}], (path, op.get('security'))


def test_a_typo_in_an_optional_key_is_flagged(api, seed):
    """The installer refuses unknown keys; better to hear it here than at the rack."""
    answer = ANSWER.replace('timezone = "Europe/Berlin"', 'timezone = "Europe/Berlin"\nreboot-on-eror = true')
    body = _admin(api, seed).post('/api/auto-install/validate', json={'answer': answer}).get_json()
    assert any('reboot-on-eror' in w for w in body['warnings']), body['warnings']
