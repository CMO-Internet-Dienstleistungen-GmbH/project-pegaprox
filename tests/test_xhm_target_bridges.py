"""The migration wizard's target lists say when a node could not be read.

`_get_pve_targets` answers which storages and bridges each target node offers. When the
node's network could not be read it used to answer ['vmbr0'] -- a bridge nobody had looked
up, offered as if it had been, so a transient API failure turned into an operator picking
the one bridge that happened to be named in the code. An empty list with the reason beside
it is what the lookup actually knows.
"""
from types import SimpleNamespace

import pegaprox.core.xhm as xhm

BRIDGES = [{'iface': 'vmbr0', 'type': 'bridge', 'active': 1},
           {'iface': 'vmbr9', 'type': 'bridge', 'active': 1},
           {'iface': 'eno1', 'type': 'eth', 'active': 1}]
STORAGES = [{'storage': 'local-lvm', 'active': 1, 'content': 'images,rootdir'}]


class _Response:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or []

    def json(self):
        return {'data': self._data}


class _Pve:
    """A Proxmox manager whose per-endpoint answers a test sets."""

    def __init__(self, network=None, storage=None):
        self.id = 'pve1'
        self.config = SimpleNamespace(name='target')
        self.host = 'pve.example'
        self.api_port = 8006
        self.nodes = {'node-a': {}}
        self._network = network or (lambda: _Response(200, BRIDGES))
        self._storage = storage or (lambda: _Response(200, STORAGES))

    def _api_get(self, url):
        return self._network() if url.endswith('/network') else self._storage()


def _raise():
    raise TimeoutError('read timed out')


def test_a_node_that_answers_lists_its_active_bridges():
    target = xhm._get_pve_targets(_Pve())[0]
    assert target['bridges'] == {'node-a': ['vmbr0', 'vmbr9']}
    assert target['bridge_errors'] == {}


def test_a_failed_bridge_lookup_offers_no_bridge_it_did_not_read():
    target = xhm._get_pve_targets(_Pve(network=_raise))[0]
    assert target['bridges'] == {'node-a': []}
    assert 'timed out' in target['bridge_errors']['node-a']


def test_a_refused_bridge_lookup_names_the_status():
    target = xhm._get_pve_targets(_Pve(network=lambda: _Response(403)))[0]
    assert target['bridges'] == {'node-a': []}
    assert target['bridge_errors'] == {'node-a': 'HTTP 403'}


def test_a_failed_storage_lookup_says_so_beside_the_empty_list():
    target = xhm._get_pve_targets(_Pve(storage=_raise))[0]
    assert target['storages'] == {'node-a': []}
    assert 'timed out' in target['storage_errors']['node-a']
    assert target['bridges'] == {'node-a': ['vmbr0', 'vmbr9']}
