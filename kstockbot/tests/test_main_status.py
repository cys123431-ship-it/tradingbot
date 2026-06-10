def test_health_shape():
    from kstockbot.app.main import read_health
    data = read_health()
    assert data["status"] == "ok"
    assert "mode" in data
    assert "mock" in data
    assert "telegram_running" in data
    assert "emergency_stop" in data
    assert "scheduler_running" in data
    assert "webhook_secret_configured" in data
    assert "webhook_fast_ack" in data
    assert "kis_credentials_configured" in data


def test_status_shape():
    from kstockbot.app.main import read_status
    data = read_status()
    assert data["status"] == "ok"
    assert "mode" in data
    assert "kis_mock" in data
    assert "daily_orders" in data
    assert "daily_order_attempts" in data
    assert "pending_approvals" in data
    assert "webhook_fast_ack" in data
    assert "runtime_dir" in data
    assert "log_dir" in data
