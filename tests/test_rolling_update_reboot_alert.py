"""Regression coverage for rolling-update reboot notifications."""


def test_reboot_event_reaches_the_alert_notification_pipeline(monkeypatch):
    from pegaprox.background import alerts

    delivered = []
    persisted = []
    monkeypatch.setattr(alerts, '_notification_handlers', [delivered.append])
    monkeypatch.setattr(alerts, '_upsert_active_alert', lambda *args: persisted.append(args))
    monkeypatch.setattr(alerts, 'load_alerts_config', lambda: {'alerts': [{
        'id': 'rolling-1', 'name': 'Rolling Updates', 'cluster_id': 'cluster_1',
        'metric': 'rolling_update', 'target_type': 'cluster', 'enabled': True,
        'channels': [],
    }]})

    assert alerts.emit_rolling_update_reboot_event('cluster_1', 'pve-01') is True

    event = delivered[0]
    assert event['alert_name'] == 'Node pve-01 rebooting for rolling update'
    assert event['cluster_id'] == 'cluster_1'
    assert event['target_type'] == 'node'
    assert event['target_name'] == 'pve-01'
    assert event['metric'] == 'rolling_update_reboot'
    assert event['severity'] == 'info'
    assert 'rolling update' in event['message']
    assert persisted[0][0] == 'rolling-1:cluster_1:node:pve-01:rolling_update'
    assert persisted[0][1] == 'rolling-1'
    assert persisted[0][-1] == 'pve-01'


def test_reboot_event_is_silent_without_an_enabled_rolling_update_alarm(monkeypatch):
    from pegaprox.background import alerts

    monkeypatch.setattr(alerts, 'load_alerts_config', lambda: {'alerts': [{
        'id': 'rolling-1', 'cluster_id': 'cluster_1', 'metric': 'rolling_update',
        'enabled': False,
    }]})
    monkeypatch.setattr(alerts, '_upsert_active_alert', lambda *args: (_ for _ in ()).throw(AssertionError()))

    assert alerts.emit_rolling_update_reboot_event('cluster_1', 'pve-01') is False


# NS Sep 2026 — added on merge of #960. The two new strings in the alert dialog were
# written as `t('rollingUpdates') || 'Rolling Updates'`, which reads like a safe
# fallback and is not: t() is `translations[lang]?.[key] || translations['en']?.[key]
# || key`, so a missing key comes back as the KEY, which is truthy, and the `||` never
# fires. Without the entries below the dialog shows the literal `rollingUpdates`.

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _translations():
    return open(os.path.join(_ROOT, 'web', 'src', 'translations.js'), encoding='utf-8').read()


def test_the_alert_dialog_strings_exist_in_every_language():
    body = _translations()
    for key in ('rollingUpdates', 'rollingUpdateAlarmHelp'):
        found = len(re.findall(rf'^\s*{key}:', body, re.M))
        assert found == 9, f'{key} is in {found} of 9 language blocks - the UI would show the key'


def test_the_option_reached_the_built_bundle():
    """web/index.html is generated from web/src; a src-only change ships nothing."""
    built = open(os.path.join(_ROOT, 'web', 'index.html'), encoding='utf-8').read()
    # the bundle is Babel output, so the JSX is gone - match the compiled element
    assert 'React.createElement("option",{value:"rolling_update"}' in built
