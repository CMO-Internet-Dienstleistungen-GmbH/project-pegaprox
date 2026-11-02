# Editing a registered Hyper-V source.
#
# The edit form was filled from the host record field by field, and the transfer address
# was not among them: it opened blank, and saving wrote the blank back over the stored
# address. An empty password field, by contrast, must keep the stored credential, because
# nobody re-types it on every edit (fork issue #15).

import os

import pytest

HOST = 'hv_1'
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORED_PASSWORD = 'fixture-' + 'stored-credential'


def _source(name):
    with open(os.path.join(REPO, 'web', 'src', name), encoding='utf-8') as fh:
        return fh.read()


@pytest.fixture
def registered_host(api, seed, monkeypatch):
    from pegaprox.core import hyperv_cluster, hyperv_db

    db = seed.db
    hyperv_db.save_host(db.conn, db._encrypt, HOST, {
        'name': 'Lab Hyper-V', 'host': 'hyperv.example', 'user': 'svc',
        'pass': STORED_PASSWORD, 'transfer_host': 'hyperv-fast.example',
        'use_ssl': True, 'auth': 'ntlm', 'smb_share_map': {'S': 'S$'},
    })
    connected_with = []

    def connect(host_id, data):
        connected_with.append(dict(data))
        return object(), None

    monkeypatch.setattr(hyperv_cluster, 'connect_hyperv_source', connect)
    monkeypatch.setattr(hyperv_cluster, 'register_hyperv_source', lambda *a, **k: None)
    return connected_with


def _stored(seed):
    from pegaprox.core import hyperv_db
    return hyperv_db.load_host(seed.db.conn, seed.db._decrypt, HOST)


class TestSavingWithoutRetypingThePassword:
    def _put(self, api, seed, body):
        admin = seed.user('root', role='admin')
        return api.as_user(admin).put(f'/api/hyperv/hosts/{HOST}', json=body)

    def test_an_absent_password_keeps_the_stored_one(self, api, seed, registered_host):
        response = self._put(api, seed, {'name': 'Lab Hyper-V', 'host': 'hyperv.example',
                                         'transfer_host': 'hyperv-fast.example'})

        assert response.status_code == 200, response.get_data(as_text=True)[:400]
        assert _stored(seed)['pass'] == STORED_PASSWORD
        assert registered_host[-1]['pass'] == STORED_PASSWORD

    def test_an_empty_password_keeps_the_stored_one(self, api, seed, registered_host):
        response = self._put(api, seed, {'name': 'Lab Hyper-V', 'host': 'hyperv.example',
                                         'pass': ''})

        assert response.status_code == 200, response.get_data(as_text=True)[:400]
        assert _stored(seed)['pass'] == STORED_PASSWORD

    def test_the_transfer_address_is_saved(self, api, seed, registered_host):
        response = self._put(api, seed, {'name': 'Lab Hyper-V', 'host': 'hyperv.example',
                                         'transfer_host': 'hyperv-other.example'})

        assert response.status_code == 200, response.get_data(as_text=True)[:400]
        assert _stored(seed)['transfer_host'] == 'hyperv-other.example'

    def test_the_listing_the_dialog_is_filled_from_carries_the_transfer_address(
            self, api, seed, registered_host):
        admin = seed.user('root', role='admin')

        hosts = api.as_user(admin).get('/api/hyperv/hosts').get_json()['hosts']

        host = next(h for h in hosts if h['id'] == HOST)
        assert host['transfer_host'] == 'hyperv-fast.example'
        assert host['use_ssl'] is True
        assert host['smb_share_map'] == {'S': 'S$'}
        assert 'pass' not in host


class TestTheEditFormShowsWhatIsStored:
    def test_the_edit_form_is_filled_with_the_transfer_address(self):
        source = _source('dashboard.js')
        start = source.index('const openHypervEdit')
        fill = source[start:source.index('setShowHypervEdit(true)', start)]

        assert 'transfer_host: host.transfer_host' in fill

    def test_every_field_the_form_sends_is_filled_when_editing(self):
        """A field the form sends but the edit does not fill is written back blank."""
        import re
        source = _source('hyperv.js')
        defaults = source[source.index('const HYPERV_DEFAULT_CONFIG'):]
        defaults = defaults[:defaults.index('};')]
        fields = set(re.findall(r'(\w+):', defaults)) - {'cluster_type'}

        dashboard = _source('dashboard.js')
        start = dashboard.index('const openHypervEdit')
        fill = dashboard[start:dashboard.index('setShowHypervEdit(true)', start)]
        filled = set(re.findall(r'(\w+):', fill))

        assert fields - filled == set()
