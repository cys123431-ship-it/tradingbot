import inspect
import pytest
import asyncio

import emas


def test_main_controller_has_all_live_advanced_alpha_methods():
    cls = getattr(emas, "MainController", None)
    assert cls is not None, "emas.MainController must exist"

    required = [
        "_fetch_live_ohlcv_rows_for_context",
        "_build_live_indicator_values",
        "_calculate_live_adx_dmi",
        "_calculate_live_htf_trend",
        "_ema_last",
        "_fetch_live_derivatives_values",
        "_fetch_live_liquidity_values",
        "_validate_live_context_or_block",
        "_fetch_futures_account_equity",
        "_normalize_live_order_plan_for_exchange",
        "_get_min_notional_for_symbol",
        "_validate_live_order_plan_for_exchange",
        "execute_live_order_plan",
        "_rebuild_plan_after_fill",
        "_place_initial_sl_from_plan",
        "_place_reduce_only_tp_from_plan",
        "_handle_sl_failure_with_persistent_pause",
        "_register_live_ladder_position_state",
        "_manage_live_ladder_exit_policy",
        "_refresh_ladder_fill_state",
        "_calculate_be_plus_fees_stop",
        "_calculate_tp1_area_stop",
        "_close_position_reduce_only_market",
    ]

    missing = [name for name in required if not hasattr(cls, name)]
    assert not missing, f"MainController is missing live helper methods: {missing}"


def test_live_advanced_alpha_async_helpers_are_coroutines():
    cls = emas.MainController

    async_methods = [
        "_fetch_live_ohlcv_rows_for_context",
        "_build_live_indicator_values",
        "_calculate_live_htf_trend",
        "_fetch_live_derivatives_values",
        "_fetch_live_liquidity_values",
        "_fetch_futures_account_equity",
        "execute_live_order_plan",
        "_place_initial_sl_from_plan",
        "_place_reduce_only_tp_from_plan",
        "_handle_sl_failure_with_persistent_pause",
        "_manage_live_ladder_exit_policy",
        "_refresh_ladder_fill_state",
        "_close_position_reduce_only_market",
    ]

    for name in async_methods:
        fn = getattr(cls, name, None)
        assert fn is not None, f"{name} missing"
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"


def test_live_advanced_alpha_binding_exact_names():
    cls = emas.MainController

    assert hasattr(cls, "_get_min_notional_for_symbol")
    assert not (
        hasattr(cls, "get_min_notional_for_symbol")
        and not hasattr(cls, "_get_min_notional_for_symbol")
    ), "Do not bind only get_min_notional_for_symbol; exact underscore method is required"

    assert hasattr(cls, "_fetch_live_ohlcv_rows_for_context")
    assert hasattr(cls, "_build_live_indicator_values")
    assert hasattr(cls, "_fetch_futures_account_equity")
    assert hasattr(cls, "execute_live_order_plan")
    assert hasattr(cls, "_register_live_ladder_position_state")
    assert hasattr(cls, "_manage_live_ladder_exit_policy")


def test_entry_live_parity_path_does_not_fail_from_missing_helpers(monkeypatch):
    async def run():
        cls = emas.MainController
        bot = cls.__new__(cls)

        bot.exchange = type("FakeExchange", (), {})()
        bot.ctrl = type("FakeCtrl", (), {})()
        bot.ctrl.is_paused = False

        async def fake_notify(msg):
            return None

        bot.ctrl.notify = fake_notify

        bot.last_entry_reason = {}
        bot.utbreakout_trailing_states = {}
        bot.position_cache = None
        bot.position_cache_time = 0

        def fake_settings():
            return {
                "mode": "testnet",
                "testnet": True,
                "live_trading": False,
                "timeframe": "15m",
                "advanced_alpha_engine_enabled": True,
                "live_parity_signal_enabled": True,
                "adaptive_ladder_tp_enabled": True,
                "live_activation_stage": "TESTNET_ONLY",
                "max_risk_per_trade_pct": 0.25,
                "require_derivatives_data_for_advanced_alpha": False,
                "require_live_dmi_for_advanced_alpha": False,
                "require_live_htf_for_advanced_alpha": False,
            }

        async def fake_fetch_rows(symbol, cfg):
            rows = []
            for i in range(220):
                price = 100.0 + i * 0.05
                rows.append({
                    "index": i,
                    "timestamp": i,
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price,
                    "volume": 1000.0 + i,
                })
            return rows

        async def fake_build_values(symbol, side, rows, idx, cfg):
            return {
                "symbol": symbol,
                "timeframe": "15m",
                "utbot_direction": str(side).upper(),
                "open": rows[idx]["open"],
                "high": rows[idx]["high"],
                "low": rows[idx]["low"],
                "close": rows[idx]["close"],
                "volume": rows[idx]["volume"],
                "adx": 30.0,
                "plus_di": 35.0,
                "minus_di": 10.0,
                "atr": 2.0,
                "atr_percentile": 50.0,
                "htf_trend": "UP",
                "supertrend_direction": "UP",
                "funding_rate": 0.0001,
                "oi_change_pct": 1.0,
                "long_short_ratio": 1.0,
                "spread_bps": 1.0,
            }

        def fake_validate(symbol, context, cfg):
            return context

        async def fake_equity(cfg):
            return 100.0

        async def fake_execute(plan, cfg):
            return {"status": "DRY_TEST_EXECUTED", "plan": plan}

        bot.get_runtime_common_settings = fake_settings
        bot._fetch_live_ohlcv_rows_for_context = fake_fetch_rows
        bot._build_live_indicator_values = fake_build_values
        bot._validate_live_context_or_block = fake_validate
        bot._fetch_futures_account_equity = fake_equity
        bot.execute_live_order_plan = fake_execute

        result = await bot.entry("BTC/USDT:USDT", "LONG", 100.0)

        assert result is None or isinstance(result, dict)

    asyncio.run(run())
