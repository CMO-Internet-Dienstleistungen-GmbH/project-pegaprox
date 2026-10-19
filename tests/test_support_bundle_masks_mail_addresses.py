"""The support bundle carries no full mail address out of the logs.

The bundle is made to leave the organisation, and its user list leaves the address out
on purpose. The log lines it copies still had them: a refused recipient, the alert
recipients of every mail sent. The log pass of the bundle now keeps the domain and masks
the rest (alice@corp.example -> ***@corp.example). Proxmox user ids such as root@pam
and an SSH target such as root@10.0.0.5 are no mail addresses and stay readable.
MK Oct 2026
"""
import io
import zipfile

from pegaprox.api import settings as settings_mod

LINES = [
    "2026-10-19 08:00:01 ERROR [SMTP] Recipients refused: alice.smith+ops@corp.example: 550 no such user\n",
    "2026-10-19 08:00:02 INFO [SMTP] Email sent successfully to ['bob@mail.corp.example']: disk full\n",
    "2026-10-19 08:00:03 INFO [API] task by root@pam on node pve1 ok\n",
    "2026-10-19 08:00:04 INFO [SSH] connected to root@10.0.0.5\n",
    "2026-10-19 08:00:05 INFO [LDAP] bind password=hunter2 for svc\n",
]


def _bundle_log(api, seed, tmp_path, monkeypatch):
    (tmp_path / 'pegaprox.log').write_text(''.join(LINES))
    monkeypatch.setattr(settings_mod, 'LOG_DIR', str(tmp_path))
    admin = api.as_user(seed.user('root', role='admin'))
    r = admin.get('/api/support-bundle')
    assert r.status_code == 200, r.data[:300]
    with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
        name = next(n for n in zf.namelist() if n.endswith('/pegaprox.log'))
        return zf.read(name).decode()


def test_the_bundle_log_masks_mail_addresses_and_keeps_what_is_none(api, seed, tmp_path, monkeypatch):
    log = _bundle_log(api, seed, tmp_path, monkeypatch)
    assert 'alice.smith+ops@' not in log and 'bob@' not in log
    assert '***@corp.example: 550 no such user' in log
    assert "['***@mail.corp.example']" in log
    # counterproof: what is no mail address stays readable, the rest of the pass still works
    assert 'task by root@pam on node pve1' in log
    assert 'connected to root@10.x.x.x' in log
    assert 'hunter2' not in log and 'password=[REDACTED]' in log
