import pytest
import os
from importlib import reload
import kstockbot.app.settings as settings_module

def test_settings_default_mode():
    # If KSTOCK_MODE is unset or invalid, it should default to 'analysis'
    os.environ["KSTOCK_MODE"] = "invalid_mode"
    reload(settings_module)
    assert settings_module.Settings.MODE == "analysis"

def test_settings_bool_parsing():
    assert settings_module.parse_bool("true") is True
    assert settings_module.parse_bool("yes") is True
    assert settings_module.parse_bool("1") is True
    assert settings_module.parse_bool("false") is False
    assert settings_module.parse_bool(None, True) is True

def test_parse_int_invalid_returns_default():
    from kstockbot.app.settings import parse_int
    assert parse_int("abc", 20) == 20
    assert parse_int("", 20) == 20
    assert parse_int("0", 20, min_value=1) == 20
    assert parse_int("999", 20, max_value=200) == 20
    assert parse_int("15", 20, min_value=1, max_value=200) == 15

def test_settings_mode_helpers():
    from kstockbot.app.settings import Settings
    old_mode = Settings.MODE
    try:
        Settings.MODE = "paper"
        assert Settings.is_paper_mode() is True
        assert Settings.is_order_capable_mode() is True
        assert Settings.is_live_like_mode() is False

        Settings.MODE = "manual_live"
        assert Settings.is_live_like_mode() is True
        assert Settings.is_order_capable_mode() is True

        Settings.MODE = "analysis"
        assert Settings.is_order_capable_mode() is False
    finally:
        Settings.MODE = old_mode


def test_webhook_fast_ack_setting_exists():
    from kstockbot.app.settings import Settings
    assert isinstance(Settings.WEBHOOK_FAST_ACK, bool)


def test_webhook_background_concurrency_setting_exists():
    from kstockbot.app.settings import Settings
    assert isinstance(Settings.WEBHOOK_BACKGROUND_MAX_CONCURRENCY, int)
    assert Settings.WEBHOOK_BACKGROUND_MAX_CONCURRENCY >= 1

