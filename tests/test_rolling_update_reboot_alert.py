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
