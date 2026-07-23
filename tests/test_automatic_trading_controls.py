import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import emas
from bot_runtime.controller_automatic_controls import (
    AUTOMATIC_SCAN_SCOPE_ALL,
    AUTOMATIC_SCAN_SCOPE_CRYPTO,
    AUTOMATIC_SCAN_SCOPE_TRADIFI,
    ControllerAutomaticTradingControlsMixin,
)
from bot_runtime.signal_automatic_controls import SignalAutomaticControlsMixin


class _ConfigStub:
    def __init__(self):
        self.config = {
            "signal_engine": {
                "automatic_trading_controls": {
                    "daily_trade_limit_base": 5,
                    "daily_trade_limit_extended": 10,
                    "daily_trade_limit_extension_utc_date": "",
                },
                "coin_selector": {
                    "scan_scope": AUTOMATIC_SCAN_SCOPE_ALL,
                    "include_tradifi_universe": True,
                },
            }
        }

    def get(self, key, default=None):
        return self.config.get(key, default)

    async def update_value(self, path, value):
        node = self.config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value


class _DBStub:
    def __init__(self, count=0):
        self.count = count

    def get_daily_entry_count(self):
        return self.count


class _ControllerStub(ControllerAutomaticTradingControlsMixin):
    def __init__(self, count=0):
        self.cfg = _ConfigStub()
        self.db = _DBStub(count)
        self.engines = {"signal": SimpleNamespace(
            coin_selector_last_result={"old": True},
            coin_selector_symbol_scores={"old": True},
            coin_selector_last_run_ts=123.0,
            coin_selector_analysis_cursor=5,
            coin_selector_strategy_cursor=4,
        )}

    async def _update_config_value(self, path, value):
        await self.cfg.update_value(path, value)


def test_daily_limit_is_five_then_extends_once_to_ten_for_utc_day():
    ctrl = _ControllerStub(count=4)
    day = datetime(2026, 7, 23, 23, 59, tzinfo=timezone.utc)

    before = ctrl.get_automatic_trading_control_status(now=day)
    assert before["effective_limit"] == 5
    assert before["remaining"] == 1
    assert before["extension_used"] is False

    changed, after = asyncio.run(
        ctrl.extend_automatic_daily_trade_limit_for_today(now=day)
    )
    assert changed is True
    assert after["effective_limit"] == 10
    assert after["remaining"] == 6
    assert after["extension_used"] is True

    changed_again, repeated = asyncio.run(
        ctrl.extend_automatic_daily_trade_limit_for_today(now=day)
    )
    assert changed_again is False
    assert repeated["effective_limit"] == 10

    next_day = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
    reset = ctrl.get_automatic_trading_control_status(now=next_day)
    assert reset["effective_limit"] == 5
    assert reset["extension_used"] is False


def test_scan_scope_persists_and_resets_automatic_selector_cache_only():
    ctrl = _ControllerStub()

    result = asyncio.run(ctrl.set_automatic_scan_scope(AUTOMATIC_SCAN_SCOPE_TRADIFI))

    assert result == AUTOMATIC_SCAN_SCOPE_TRADIFI
    coin_cfg = ctrl.cfg.config["signal_engine"]["coin_selector"]
    assert coin_cfg["scan_scope"] == AUTOMATIC_SCAN_SCOPE_TRADIFI
    assert coin_cfg["include_tradifi_universe"] is True
    engine = ctrl.engines["signal"]
    assert engine.coin_selector_last_result == {}
    assert engine.coin_selector_symbol_scores == {}
    assert engine.coin_selector_last_run_ts == 0.0

    asyncio.run(ctrl.set_automatic_scan_scope(AUTOMATIC_SCAN_SCOPE_CRYPTO))
    assert coin_cfg["scan_scope"] == AUTOMATIC_SCAN_SCOPE_CRYPTO
    assert coin_cfg["include_tradifi_universe"] is False


def test_telegram_keyboard_contains_daily_limit_and_all_scan_scope_buttons():
    ctrl = _ControllerStub()
    keyboard = ctrl._build_automatic_trading_controls_keyboard()
    callback_data = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert "atc:limit:extend" in callback_data
    assert "atc:scope:tradfi_only" in callback_data
    assert "atc:scope:crypto_only" in callback_data
    assert "atc:scope:all_allowed" in callback_data


def _market(base, contract_type):
    return {
        "symbol": f"{base}/USDT:USDT",
        "id": f"{base}USDT",
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "active": True,
        "type": "swap",
        "info": {
            "symbol": f"{base}USDT",
            "contractType": contract_type,
            "status": "TRADING",
        },
    }


class _ScopeController:
    def __init__(self, scope):
        self.scope = scope
        self.markets = {
            "BTC/USDT:USDT": _market("BTC", "PERPETUAL"),
            "QQQ/USDT:USDT": _market("QQQ", "TRADIFI_PERPETUAL"),
        }

    def get_automatic_scan_scope(self):
        return self.scope

    def get_exchange_mode(self):
        return emas.BINANCE_MAINNET

    async def _load_trade_markets_for_exchange_mode(self, mode):
        return self.markets

    def _futures_market_for_symbol(self, symbol, markets):
        return markets.get(symbol)


class _ScopeEngine(SignalAutomaticControlsMixin):
    def __init__(self, scope):
        self.ctrl = _ScopeController(scope)

    def is_upbit_mode(self):
        return False


@pytest.mark.parametrize(
    "scope,allowed,blocked,code",
    [
        (
            AUTOMATIC_SCAN_SCOPE_TRADIFI,
            "QQQ/USDT:USDT",
            "BTC/USDT:USDT",
            "REJECTED_SCAN_SCOPE_CRYPTO",
        ),
        (
            AUTOMATIC_SCAN_SCOPE_CRYPTO,
            "BTC/USDT:USDT",
            "QQQ/USDT:USDT",
            "REJECTED_SCAN_SCOPE_TRADIFI",
        ),
    ],
)
def test_final_automatic_entry_scope_gate_allows_only_selected_universe(
    scope, allowed, blocked, code
):
    engine = _ScopeEngine(scope)

    assert asyncio.run(engine._assert_automatic_entry_scan_scope(allowed)) == allowed
    with pytest.raises(ValueError, match=code):
        asyncio.run(engine._assert_automatic_entry_scan_scope(blocked))


def test_all_allowed_scope_accepts_crypto_and_tradfi():
    engine = _ScopeEngine(AUTOMATIC_SCAN_SCOPE_ALL)
    assert asyncio.run(
        engine._assert_automatic_entry_scan_scope("BTC/USDT:USDT")
    ) == "BTC/USDT:USDT"
    assert asyncio.run(
        engine._assert_automatic_entry_scan_scope("QQQ/USDT:USDT")
    ) == "QQQ/USDT:USDT"


def test_coin_selector_scope_reject_codes_match_selected_scope():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)

    assert engine._coin_selector_scope_reject_code(
        {"scan_scope": AUTOMATIC_SCAN_SCOPE_TRADIFI}, False
    ) == "REJECTED_SCAN_SCOPE_CRYPTO"
    assert engine._coin_selector_scope_reject_code(
        {"scan_scope": AUTOMATIC_SCAN_SCOPE_TRADIFI}, True
    ) is None
    assert engine._coin_selector_scope_reject_code(
        {"scan_scope": AUTOMATIC_SCAN_SCOPE_CRYPTO}, True
    ) == "REJECTED_SCAN_SCOPE_TRADIFI"
    assert engine._coin_selector_scope_reject_code(
        {"scan_scope": AUTOMATIC_SCAN_SCOPE_ALL}, False
    ) is None


def test_config_defaults_persist_automatic_controls_and_scan_scope(tmp_path):
    cfg = emas.TradingConfig(str(tmp_path / "config.json"))
    signal_cfg = cfg.get("signal_engine", {})

    assert signal_cfg["automatic_trading_controls"] == {
        "daily_trade_limit_base": 5,
        "daily_trade_limit_extended": 10,
        "daily_trade_limit_extension_utc_date": "",
    }
    assert signal_cfg["coin_selector"]["scan_scope"] == AUTOMATIC_SCAN_SCOPE_ALL


def test_daily_automatic_count_excludes_user_custom_entries(tmp_path):
    db = emas.DBManager(str(tmp_path / "trades.db"))
    db.log_trade_entry(
        "BTC/USDT:USDT", "long", 100.0, 0.1, strategy="ut_breakout"
    )
    db.log_trade_entry(
        "ETH/USDT:USDT", "short", 100.0, 0.1, strategy="user_custom"
    )

    assert db.get_daily_entry_count() == 2
    assert db.get_daily_automatic_entry_count() == 1


def test_final_live_gateway_applies_scope_to_automatic_but_not_user_custom(monkeypatch):
    from bot_runtime import live_orders

    async def tradeable(symbol):
        return symbol

    automatic_scope = __import__("unittest.mock").mock.AsyncMock(
        side_effect=ValueError("REJECTED_SCAN_SCOPE_TRADIFI")
    )
    owner = SimpleNamespace(
        ctrl=SimpleNamespace(_assert_symbol_tradeable_in_current_exchange_mode=tradeable),
        _assert_automatic_entry_scan_scope=automatic_scope,
    )
    automatic_plan = SimpleNamespace(
        symbol="QQQ/USDT:USDT", side="long", engine="UTBREAK"
    )
    with pytest.raises(ValueError, match="REJECTED_SCAN_SCOPE_TRADIFI"):
        asyncio.run(live_orders.execute_live_order_plan(owner, automatic_plan, {}))
    automatic_scope.assert_awaited_once()

    automatic_scope.reset_mock()
    monkeypatch.setattr(
        live_orders,
        "enforce_activation_stage",
        lambda cfg: (_ for _ in ()).throw(RuntimeError("AFTER_AUTOMATIC_GATE")),
    )
    custom_plan = SimpleNamespace(
        symbol="QQQ/USDT:USDT", side="long", engine="USER_CUSTOM"
    )
    with pytest.raises(RuntimeError, match="AFTER_AUTOMATIC_GATE"):
        asyncio.run(live_orders.execute_live_order_plan(owner, custom_plan, {}))
    automatic_scope.assert_not_awaited()


def test_final_live_gateway_rechecks_automatic_daily_limit():
    from bot_runtime import live_orders

    async def tradeable(symbol):
        return symbol

    async def scope_allowed(symbol):
        return symbol

    owner = SimpleNamespace(
        ctrl=SimpleNamespace(_assert_symbol_tradeable_in_current_exchange_mode=tradeable),
        _assert_automatic_entry_scan_scope=scope_allowed,
        get_effective_automatic_daily_trade_limit=lambda: 5,
        get_automatic_daily_entry_count=lambda: 5,
    )
    plan = SimpleNamespace(symbol="BTC/USDT:USDT", side="long", engine="UTBREAK")

    with pytest.raises(emas.TradingSafetyError, match="REJECTED_DAILY_TRADE_LIMIT"):
        asyncio.run(live_orders.execute_live_order_plan(owner, plan, {}))


def test_effective_strategy_config_uses_todays_extended_limit():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.ctrl = SimpleNamespace(
        get_effective_automatic_daily_trade_limit=lambda: 10
    )

    cfg = engine._get_utbot_filtered_breakout_config(
        {
            "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
            "UTBotFilteredBreakoutV1": {"max_daily_trades": 5},
        }
    )

    assert cfg["max_daily_trades"] == 10


def test_concurrent_extension_requests_only_succeed_once():
    ctrl = _ControllerStub()
    day = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)

    async def run_both():
        return await asyncio.gather(
            ctrl.extend_automatic_daily_trade_limit_for_today(now=day),
            ctrl.extend_automatic_daily_trade_limit_for_today(now=day),
        )

    results = asyncio.run(run_both())
    assert sorted(changed for changed, _ in results) == [False, True]


def test_tradfi_universe_inclusion_follows_scan_scope_on_mainnet():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.is_upbit_mode = lambda: False
    engine.ctrl = SimpleNamespace(get_exchange_mode=lambda: emas.BINANCE_MAINNET)

    assert engine._coin_selector_should_include_tradifi_universe(
        {"scan_scope": AUTOMATIC_SCAN_SCOPE_TRADIFI, "include_tradifi_universe": False},
        custom_enabled=False,
    ) is True
    assert engine._coin_selector_should_include_tradifi_universe(
        {"scan_scope": AUTOMATIC_SCAN_SCOPE_CRYPTO, "include_tradifi_universe": True},
        custom_enabled=False,
    ) is False
