# Regression guards for the Aikido Testing-branch pentest fixes (batch 2/2, Aug 2026, NS).
# These pin the authz / validation / SSRF invariants the batch-2 commit added, driven through
# the real Flask app (deny path fires before any manager is touched) plus a couple of pure-unit
# checks for the helpers that don't need HTTP.

import ipaddress
import socket

import pytest

from pegaprox.utils import url_security
from pegaprox.utils.url_security import resolve_and_pin_url, SsrfError


# ---------------------------------------------------------------------------
# storage download-url DNS-rebind pin — resolve_and_pin_url
# ---------------------------------------------------------------------------

def _resolver(mapping):
    def _r(host):
        if host not in mapping:
            raise socket.gaierror('unresolvable in test')
        return [ipaddress.ip_address(ip) for ip in mapping[host]]
    return _r


def test_pin_http_rewrites_host_to_validated_ip(monkeypatch):
    # A delegated http download must be pinned to the exact IP we vetted so the PVE node can't
    # re-resolve the hostname to a private address (DNS rebind). Path + port are preserved.
    monkeypatch.setattr(url_security, '_resolve_all', _resolver({'mirror.example': ['93.184.216.34']}))
    out = resolve_and_pin_url('http://mirror.example:8080/debian.iso', allowed_schemes=('https', 'http'))
    assert out == 'http://93.184.216.34:8080/debian.iso'


def test_pin_https_keeps_hostname(monkeypatch):
    # https is left as the hostname on purpose — the fetcher's TLS cert check is bound to the
    # name, so a rebind to an internal IP fails the handshake; rewriting would break legit certs.
    monkeypatch.setattr(url_security, '_resolve_all', _resolver({'mirror.example': ['93.184.216.34']}))
    out = resolve_and_pin_url('https://mirror.example/debian.iso', allowed_schemes=('https', 'http'))
    assert out == 'https://mirror.example/debian.iso'


def test_pin_http_rejects_private_only_host(monkeypatch):
    monkeypatch.setattr(url_security, '_resolve_all', _resolver({'evil.example': ['10.0.0.5']}))
    with pytest.raises(SsrfError):
        resolve_and_pin_url('http://evil.example/x.iso', allowed_schemes=('https', 'http'))


def test_pin_http_rejects_metadata_rebind(monkeypatch):
    monkeypatch.setattr(url_security, '_resolve_all', _resolver({'rebind.example': ['169.254.169.254']}))
    with pytest.raises(SsrfError):
        resolve_and_pin_url('http://rebind.example/latest/meta-data/', allowed_schemes=('https', 'http'))


def test_pin_ip_literal_http_unchanged(monkeypatch):
    # already an IP literal → nothing to rebind, and the resolver must not even be consulted.
    def _boom(host):
        raise AssertionError('resolver must not run for an IP literal')
    monkeypatch.setattr(url_security, '_resolve_all', _boom)
    out = resolve_and_pin_url('http://93.184.216.34/x.iso', allowed_schemes=('https', 'http'))
    assert out == 'http://93.184.216.34/x.iso'


# ---------------------------------------------------------------------------
# power rates — only a global admin may overwrite the shared __default__ row
# ---------------------------------------------------------------------------

def test_cluster_config_holder_cannot_write_default_power_rates(api, seed):
    # tenant-scoped holder of cluster.config (restricted to their own clusters) must NOT be able
    # to tamper with the __default__ fallback row every other cluster reads.
    seed.tenant('tenant_a', clusters=['cluster_home'])
    alice = seed.user('alice', role='user', tenant_id='tenant_a', permissions=['cluster.config'])
    r = api.as_user(alice).put('/api/power/rates/__default__', json={'kwh_price': 0.01})
    assert r.status_code == 403, r.get_data(as_text=True)


def test_admin_can_write_default_power_rates(api, seed):
    root = seed.user('root', role='admin', tenant_id='default')
    r = api.as_user(root).put('/api/power/rates/__default__', json={'kwh_price': 0.30})
    assert r.status_code != 403, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# cluster-groups — a global (tenant_id NULL) group is admin-only for writes
# ---------------------------------------------------------------------------

def _seed_global_group(seed, gid='g-global'):
    seed.db.execute(
        'INSERT INTO cluster_groups (id, name, description, color, tenant_id, sort_order, '
        'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (gid, 'Global Group', '', '#ffffff', None, 0, '2026-01-01T00:00:00', '2026-01-01T00:00:00'))
    return gid


def test_tenant_admin_groups_holder_cannot_delete_global_group(api, seed):
    gid = _seed_global_group(seed)
    seed.tenant('tenant_a', clusters=['cluster_home'])
    alice = seed.user('alice', role='user', tenant_id='tenant_a', permissions=['admin.groups'])
    r = api.as_user(alice).delete(f'/api/cluster-groups/{gid}')
    assert r.status_code == 403, r.get_data(as_text=True)


def test_tenant_cluster_config_holder_cannot_balance_global_group(api, seed):
    gid = _seed_global_group(seed, 'g-global-2')
    seed.tenant('tenant_a', clusters=['cluster_home'])
    alice = seed.user('alice', role='user', tenant_id='tenant_a', permissions=['cluster.config'])
    r = api.as_user(alice).post(f'/api/cluster-groups/{gid}/balance-now')
    assert r.status_code == 403, r.get_data(as_text=True)


def test_admin_can_delete_global_group(api, seed):
    gid = _seed_global_group(seed, 'g-global-3')
    root = seed.user('root', role='admin', tenant_id='default')
    r = api.as_user(root).delete(f'/api/cluster-groups/{gid}')
    assert r.status_code == 200, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# vm-tags — a non-numeric vmid must be rejected BEFORE the global DELETE+rewrite
# ---------------------------------------------------------------------------

def test_non_numeric_vmid_rejected_on_tag_update(api, seed):
    # admin passes check_cluster_access, so we reach (and prove) the numeric guard rather than a
    # cluster-access 403. A bad vmid used to ValueError mid-rewrite and persist a table wipe.
    root = seed.user('root', role='admin', tenant_id='default')
    api.set_manager('cluster_1', api.make_fake_manager())
    r = api.as_user(root).post('/api/clusters/cluster_1/vms/abc/tags', json={'tags': [{'name': 'x'}]})
    assert r.status_code == 400, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Prometheus exporter — only an ADMIN-role token may scrape the global feed
# ---------------------------------------------------------------------------

def test_metrics_rejects_non_admin_token(api, monkeypatch):
    import pegaprox.api.metrics_exporter as mx
    monkeypatch.setattr(mx, 'validate_api_token', lambda tok: {'user': 'svc', 'role': 'viewer'})
    r = api.anon().get('/api/metrics', headers={'Authorization': 'Bearer pgx_dummy'})
    assert r.status_code == 401, r.get_data(as_text=True)


def test_metrics_allows_admin_token(api, monkeypatch):
    import pegaprox.api.metrics_exporter as mx
    monkeypatch.setattr(mx, 'validate_api_token', lambda tok: {'user': 'svc', 'role': 'admin'})
    r = api.anon().get('/api/metrics', headers={'Authorization': 'Bearer pgx_dummy'})
    assert r.status_code == 200, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# ws-token validate — the node SSH shell path (shell=node) must enforce node.shell
# ---------------------------------------------------------------------------

def _ws_token(user, role):
    from pegaprox.utils.realtime import create_ws_token
    return create_ws_token(user, role)


def test_node_shell_ws_requires_node_shell_permission(api, seed):
    # alice reaches cluster_1 (her tenant owns it) but has no node.shell → the standalone SSH
    # shell server's validate call (shell=node) must 403, not just wave the cluster grant through.
    seed.tenant('tenant_a', clusters=['cluster_1'])
    seed.user('alice', role='viewer', tenant_id='tenant_a')
    tok = _ws_token('alice', 'viewer')
    r = api.anon().get(f'/api/ws/token/validate?token={tok}&cluster_id=cluster_1&node=pve1&shell=node')
    assert r.status_code == 403, r.get_data(as_text=True)
    assert 'node.shell' in r.get_data(as_text=True)


def test_vm_termproxy_ws_not_gated_on_node_shell(api, seed):
    # the VM termproxy path does NOT send shell=node — the same cluster-only grant must still be
    # accepted there (guest console is gated separately), i.e. NOT a node.shell 403.
    seed.tenant('tenant_a', clusters=['cluster_1'])
    seed.user('alice', role='viewer', tenant_id='tenant_a')
    tok = _ws_token('alice', 'viewer')
    r = api.anon().get(f'/api/ws/token/validate?token={tok}&cluster_id=cluster_1&node=pve1')
    assert r.status_code != 403, r.get_data(as_text=True)


def test_node_shell_ws_allows_holder(api, seed):
    # positive control: an admin (holds node.shell) is not blocked by the new gate.
    seed.user('root', role='admin', tenant_id='default')
    tok = _ws_token('root', 'admin')
    r = api.anon().get(f'/api/ws/token/validate?token={tok}&cluster_id=cluster_1&node=pve1&shell=node')
    assert r.status_code != 403, r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# multi-sdn create/purge TOCTOU — in-flight provisioning advert is observed
# ---------------------------------------------------------------------------

def test_provisioning_advert_marks_shared_infra():
    from pegaprox.api import multi_sdn
    tok = multi_sdn._register_provisioning(
        ['cluster_1', 'cluster_2'], {'zone': 'evpnz', 'controller': 'evpnc'})
    try:
        # a concurrent purge on a shared member sees the in-flight zone+controller as shared
        assert multi_sdn._provisioning_shares('cluster_1', 'evpnz', 'evpnc') == (True, True)
        # a non-member cluster is unaffected
        assert multi_sdn._provisioning_shares('cluster_9', 'evpnz', 'evpnc') == (False, False)
        # partial match: only the controller name lines up
        assert multi_sdn._provisioning_shares('cluster_2', 'other', 'evpnc') == (False, True)
    finally:
        multi_sdn._unregister_provisioning(tok)
    # once the create records its DB row and unregisters, the advert is gone
    assert multi_sdn._provisioning_shares('cluster_1', 'evpnz', 'evpnc') == (False, False)
