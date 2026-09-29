"""Automated installations — PegaProx as the answer-file server for the Proxmox
auto-installer.

The shape of the feature: an operator stores an answer file, PegaProx hands out a
fetch token once, the prepared ISO POSTs to /api/auto-install/answer with that
token and gets the file back. What the ISO reports afterwards lands in the run
list, which is the only reason anyone can say "three of the five are done" without
walking to the rack.

Everything here drives the real Flask app. The installer half in particular is
tested through a RAW test client, because the harness client adds the same-origin
and XHR headers a real installer cannot send — with those headers on, this suite
would pass even if the CSRF gate rejected every installer in production.
"""
import json

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

SYSINFO = {
    'product': 'pve',
    'version': '9.2',
    'dmi': {'system': {'manufacturer': 'Dell Inc.', 'product': 'PowerEdge R660', 'serial': 'JH7K2X3'}},
    'network_interfaces': [{'name': 'eno1', 'mac': 'aa:bb:cc:dd:ee:01'}],
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
    """A client with none of the browser headers — this is what the installer is."""
    return api.app.test_client()


def _fetch(api, token, info=None, **kw):
    return _raw(api).post(f'/api/auto-install/answer?token={token}',
                          json=info if info is not None else SYSINFO,
                          base_url='http://localhost', **kw)


# --- the token -------------------------------------------------------------

def test_the_token_is_handed_out_once_and_never_again(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    token = created['token']
    assert token.startswith('pgxai_')

    again = c.get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert 'token' not in again, again
    listed = c.get('/api/auto-install/profiles').get_json()
    assert all('token' not in p for p in listed['profiles'])
    assert listed['can_manage'] is True
    # and the hint must not be enough to reconstruct it
    assert len(again.get('token_hint') or '') < len(token)


def test_the_token_is_not_stored_in_the_clear(api, seed):
    """A DB read must not hand somebody a working token."""
    c = _admin(api, seed)
    token = _create(c)['token']
    from pegaprox.core.db import get_db
    row = get_db().conn.cursor().execute(
        'SELECT token_hash FROM auto_install_profiles').fetchone()
    assert token not in row['token_hash']
    import hashlib
    assert row['token_hash'] == hashlib.sha256(token.encode()).hexdigest()


def test_rotating_the_token_stops_the_old_one(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    old = created['token']
    assert _fetch(api, old).status_code == 200

    new = c.post(f"/api/auto-install/profiles/{created['id']}/token").get_json()['token']
    assert new != old
    assert _fetch(api, old).status_code == 403
    assert _fetch(api, new).status_code == 200


# --- serving the answer file ----------------------------------------------

def test_the_installer_gets_the_answer_file_without_a_session(api, seed):
    """No X-Session-ID, no Origin, no X-Requested-With — the way an ISO arrives."""
    token = _create(_admin(api, seed))['token']
    r = _fetch(api, token)
    assert r.status_code == 200, r.data
    body = r.data.decode()
    assert r.headers['Content-Type'] == 'text/plain; charset=utf-8', r.headers['Content-Type']
    assert 'pve01.lab.example.com' in body
    assert body.count('[global]') == 1


def test_the_served_file_carries_a_callback_the_installer_can_reach(api, seed):
    token = _create(_admin(api, seed))['token']
    body = _fetch(api, token).data.decode()
    assert '[post-installation-webhook]' in body
    assert '/api/auto-install/progress' in body
    assert token in body

    import tomllib
    hook = tomllib.loads(body)['post-installation-webhook']
    assert hook['url'].startswith('http://localhost/api/auto-install/progress?token=')


def test_an_answer_file_with_its_own_webhook_section_is_replaced_not_duplicated(api, seed):
    """Two [post-installation-webhook] tables is a TOML error, so a naive append
    would hand the installer a file it refuses to parse."""
    own = ANSWER + '\n[post-installation-webhook]\nurl = "https://elsewhere.invalid/hook"\n'
    token = _create(_admin(api, seed), answer=own)['token']
    body = _fetch(api, token).data.decode()
    assert body.count('[post-installation-webhook]') == 1
    import tomllib
    assert 'elsewhere.invalid' not in tomllib.loads(body)['post-installation-webhook']['url']


def test_a_section_after_the_webhook_survives_the_strip(api, seed):
    own = (ANSWER + '\n[post-installation-webhook]\nurl = "https://elsewhere.invalid/hook"\n'
           + '\n[first-boot]\nsource = "from-url"\nurl = "https://example.com/fb"\n')
    token = _create(_admin(api, seed), answer=own)['token']
    body = _fetch(api, token).data.decode()
    import tomllib
    parsed = tomllib.loads(body)
    assert parsed['first-boot']['url'] == 'https://example.com/fb'


def test_a_bad_token_is_refused_and_no_run_is_recorded(api, seed):
    c = _admin(api, seed)
    _create(c)
    r = _fetch(api, 'pgxai_not-a-real-token')
    assert r.status_code == 403
    assert b'pve01' not in r.data
    assert c.get('/api/auto-install/runs').get_json() == []


def test_a_missing_token_is_refused(api, seed):
    _create(_admin(api, seed))
    r = _raw(api).post('/api/auto-install/answer', json=SYSINFO, base_url='http://localhost')
    assert r.status_code == 403


@pytest.mark.parametrize('field,value', [
    ('enabled', False),
    ('expires_at', '2001-01-01T00:00:00'),
])
def test_a_profile_that_is_off_or_expired_serves_nothing(api, seed, field, value):
    c = _admin(api, seed)
    created = _create(c)
    token = created['token']
    body = {'name': created['name'], 'answer': ANSWER, field: value}
    if field != 'enabled':
        body['enabled'] = True
    assert c.put(f"/api/auto-install/profiles/{created['id']}", json=body).status_code == 200
    assert _fetch(api, token).status_code == 403


def test_the_use_limit_is_enforced(api, seed):
    c = _admin(api, seed)
    created = _create(c, max_uses=1)
    token = created['token']
    assert _fetch(api, token).status_code == 200
    assert _fetch(api, token).status_code == 403


# --- runs ------------------------------------------------------------------

def test_a_fetch_shows_up_as_an_installing_machine(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    _fetch(api, token)
    runs = c.get('/api/auto-install/runs').get_json()
    assert len(runs) == 1
    assert runs[0]['status'] == 'installing'
    assert runs[0]['product'] == 'Dell Inc. PowerEdge R660'
    assert runs[0]['fingerprint'] == 'serial:JH7K2X3'
    assert runs[0]['profile_name'] == 'rack-7 node'


def test_the_same_machine_retrying_does_not_become_two_machines(api, seed):
    c = _admin(api, seed)
    token = _create(c, max_uses=0)['token']
    _fetch(api, token)
    _fetch(api, token)
    assert len(c.get('/api/auto-install/runs').get_json()) == 1


def test_two_machines_on_one_profile_are_two_runs(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    _fetch(api, token)
    other = json.loads(json.dumps(SYSINFO))
    other['dmi']['system']['serial'] = 'OTHER-9'
    _fetch(api, token, info=other)
    assert len(c.get('/api/auto-install/runs').get_json()) == 2


def test_an_oversized_inventory_is_kept_readable(api, seed):
    """Cutting the JSON string in half stores something the run list cannot parse,
    so the whole inventory silently becomes {} - worse than saying it was dropped."""
    c = _admin(api, seed)
    token = _create(c)['token']
    fat = dict(SYSINFO)
    fat['dmesg'] = 'x' * 200000
    _fetch(api, token, info=fat)
    run = c.get('/api/auto-install/runs').get_json()[0]
    assert run['system_info'].get('truncated') is True, run['system_info']
    assert 'dmesg' in run['system_info']['keys']
    assert run['product'] == 'Dell Inc. PowerEdge R660'      # the summary still works


def test_hardware_with_no_usable_serial_falls_back_to_the_mac(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    info = json.loads(json.dumps(SYSINFO))
    info['dmi']['system']['serial'] = 'To be filled by O.E.M.'
    _fetch(api, token, info=info)
    assert c.get('/api/auto-install/runs').get_json()[0]['fingerprint'] == 'mac:aa:bb:cc:dd:ee:01'


@pytest.mark.parametrize('payload,expected', [
    ({'status': 'installed'}, 'installed'),
    ({'status': 'ok'}, 'installed'),
    ({'success': True}, 'installed'),
    ({'success': False, 'message': 'disk too small'}, 'failed'),
    ({'status': 'error', 'error': 'no nic'}, 'failed'),
    ({'error': 'boom'}, 'failed'),
    ({'something': 'else'}, 'installing'),
])
def test_the_webhook_moves_the_run_to_its_outcome(api, seed, payload, expected):
    c = _admin(api, seed)
    token = _create(c)['token']
    _fetch(api, token)
    r = _raw(api).post(f'/api/auto-install/progress?token={token}',
                       json=payload, base_url='http://localhost')
    assert r.status_code == 200, r.data
    run = c.get('/api/auto-install/runs').get_json()[0]
    assert run['status'] == expected


def test_a_finished_install_can_still_report_after_the_token_is_spent(api, seed):
    """max_uses=1 means the ISO cannot fetch again — it must still be able to say
    it is done, or the run sits at 'installing' for ever."""
    c = _admin(api, seed)
    token = _create(c, max_uses=1)['token']
    _fetch(api, token)
    r = _raw(api).post(f'/api/auto-install/progress?token={token}',
                       json={'status': 'installed'}, base_url='http://localhost')
    assert r.status_code == 200, r.data
    assert c.get('/api/auto-install/runs').get_json()[0]['status'] == 'installed'


def test_progress_from_an_unknown_token_is_refused(api, seed):
    c = _admin(api, seed)
    token = _create(c)['token']
    _fetch(api, token)
    r = _raw(api).post('/api/auto-install/progress?token=pgxai_nope',
                       json={'status': 'failed'}, base_url='http://localhost')
    assert r.status_code == 403
    assert c.get('/api/auto-install/runs').get_json()[0]['status'] == 'installing'


# --- the root password -----------------------------------------------------

def test_the_answer_file_is_encrypted_at_rest(api, seed):
    """It contains a root password. A DB column anyone can read is not where it goes."""
    _create(_admin(api, seed))
    from pegaprox.core.db import get_db
    stored = get_db().conn.cursor().execute(
        'SELECT answer_encrypted FROM auto_install_profiles').fetchone()['answer_encrypted']
    assert 'hunter2-in-the-rack' not in stored
    assert get_db()._decrypt(stored) == ANSWER


def test_view_only_sees_the_shape_of_the_file_not_the_password(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    watcher = api.as_user(seed.user('watcher', role='viewer', permissions=['autoinstall.view']))
    got = watcher.get(f"/api/auto-install/profiles/{created['id']}")
    assert got.status_code == 200, got.data
    body = got.get_json()
    assert body['answer_redacted'] is True
    assert 'hunter2-in-the-rack' not in body['answer']
    assert 'root-password' in body['answer']          # still recognisable as an answer file
    assert 'pve01.lab.example.com' in body['answer']
    assert watcher.get('/api/auto-install/profiles').get_json()['can_manage'] is False


def test_view_only_cannot_create_or_rotate(api, seed):
    admin = _admin(api, seed)
    created = _create(admin)
    watcher = api.as_user(seed.user('watcher', role='viewer', permissions=['autoinstall.view']))
    assert watcher.post('/api/auto-install/profiles',
                        json={'name': 'x', 'answer': ANSWER}).status_code == 403
    assert watcher.post(f"/api/auto-install/profiles/{created['id']}/token").status_code == 403
    assert watcher.delete(f"/api/auto-install/profiles/{created['id']}").status_code == 403


def test_a_user_without_either_permission_sees_nothing(api, seed):
    _create(_admin(api, seed))
    nobody = api.as_user(seed.user('nobody', role='user'))
    assert nobody.get('/api/auto-install/profiles').status_code == 403
    assert nobody.get('/api/auto-install/runs').status_code == 403


# --- validation ------------------------------------------------------------

@pytest.mark.parametrize('answer,needle', [
    ('not toml at all {{{', 'Not valid TOML'),
    ('[global]\nkeyboard = "de"\n', 'country'),
    (ANSWER.replace('[disk-setup]\nfilesystem = "ext4"\ndisk-list = ["sda"]\n', ''), 'disk-setup'),
    (ANSWER.replace('source = "from-dhcp"', 'source = "magic"'), 'from-dhcp'),
    (ANSWER.replace('fqdn = "pve01.lab.example.com"', 'fqdn = "pve01"'), 'fully qualified'),
])
def test_the_validator_catches_what_the_installer_would_catch_at_3am(api, seed, answer, needle):
    c = _admin(api, seed)
    r = c.post('/api/auto-install/validate', json={'answer': answer})
    body = r.get_json()
    assert body['valid'] is False
    assert any(needle in e for e in body['errors']), body['errors']


def test_a_good_answer_file_validates(api, seed):
    body = _admin(api, seed).post('/api/auto-install/validate', json={'answer': ANSWER}).get_json()
    assert body['valid'] is True
    assert body['errors'] == []
    assert any('clear text' in w for w in body['warnings'])


def test_an_invalid_answer_file_cannot_be_saved(api, seed):
    c = _admin(api, seed)
    r = c.post('/api/auto-install/profiles', json={'name': 'broken', 'answer': '[global]\n'})
    assert r.status_code == 400
    assert c.get('/api/auto-install/profiles').get_json()['profiles'] == []


# --- the callback address --------------------------------------------------

@pytest.mark.parametrize('host', [
    'evil.example.com"',            # closes the TOML string the URL sits in
    'host with spaces',
])
def test_a_hostile_host_header_does_not_reach_the_answer_file(api, seed, host):
    """The Host header lands in a TOML string. It only ever comes from the machine
    being installed, but 'it can only hurt itself' is not a parser — and an answer
    file that fails to parse is a machine stuck at an installer prompt.

    A host we don't recognise costs the callback section, not the install. Only the
    two values below actually reach the handler; werkzeug answers a Host with a path
    in it with a 400 of its own and throws on an unparsable port, so testing those
    here would only be testing werkzeug."""
    token = _create(_admin(api, seed))['token']
    r = _raw(api).post(f'/api/auto-install/answer?token={token}', json=SYSINFO,
                       base_url='http://localhost', headers={'Host': host})
    assert r.status_code == 200, r.data
    import tomllib
    parsed = tomllib.loads(r.data.decode())
    assert parsed['global']['root-password'] == 'hunter2-in-the-rack'
    assert 'post-installation-webhook' not in parsed   # dropped rather than guessed


def test_an_explicit_callback_url_wins(api, seed):
    c = _admin(api, seed)
    created = _create(c, callback_url='https://pegaprox.example.com/api/auto-install/progress')
    body = _fetch(api, created['token']).data.decode()
    import tomllib
    assert tomllib.loads(body)['post-installation-webhook']['url'].startswith(
        'https://pegaprox.example.com/api/auto-install/progress?token=')


def test_a_fingerprint_has_to_look_like_one(api, seed):
    c = _admin(api, seed)
    r = c.post('/api/auto-install/profiles',
               json={'name': 'x', 'answer': ANSWER, 'callback_fingerprint': 'AB:CD'})
    assert r.status_code == 400
    assert 'fingerprint' in r.get_json()['error']


# --- lifecycle -------------------------------------------------------------

def test_editing_without_resending_the_answer_keeps_it(api, seed):
    """The UI can show a redacted file; a save from that screen must not write the
    redaction back over the real password."""
    c = _admin(api, seed)
    created = _create(c)
    r = c.put(f"/api/auto-install/profiles/{created['id']}",
              json={'name': 'renamed', 'description': 'now with a name'})
    assert r.status_code == 200, r.data
    full = c.get(f"/api/auto-install/profiles/{created['id']}").get_json()
    assert full['name'] == 'renamed'
    assert 'hunter2-in-the-rack' in full['answer']


def test_deleting_a_profile_takes_its_runs_with_it(api, seed):
    c = _admin(api, seed)
    created = _create(c)
    _fetch(api, created['token'])
    assert len(c.get('/api/auto-install/runs').get_json()) == 1
    assert c.delete(f"/api/auto-install/profiles/{created['id']}").status_code == 200
    assert c.get('/api/auto-install/runs').get_json() == []
    assert _fetch(api, created['token']).status_code == 403
