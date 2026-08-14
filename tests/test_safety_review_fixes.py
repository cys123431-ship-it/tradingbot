import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _emas_module():
    return pytest.importorskip(
        "emas",
        reason="emas runtime dependencies are optional in CI",
    )


def test_daily_loss_breaker_uses_tighter_percentage_limit():
    emas = _emas_module()
    engine = emas.BaseEngine.__new__(emas.BaseEngine)
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(status_data={}, notify=AsyncMock())
    engine.db = SimpleNamespace(get_daily_stats=lambda: (1, -300.0))
    engine.get_runtime_common_settings = lambda: {
        "daily_loss_limit": 100_000.0,
        "daily_loss_limit_pct": 5.0,
    }
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(True, set())
    )
    engine._small_account_adaptive_trend_daily_loss_exempt = AsyncMock(
        return_value=False
    )
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))

    assert asyncio.run(engine.check_daily_loss_limit()) is True
    engine.db.get_daily_stats = lambda: (1, -250.0)
    assert asyncio.run(engine.check_daily_loss_limit()) is True
    engine.db.get_daily_stats = lambda: (1, -200.0)
    assert asyncio.run(engine.check_daily_loss_limit()) is False


def test_daily_loss_ignores_stale_unrealized_pnl_after_confirmed_flat_snapshot():
    emas = _emas_module()
    engine = emas.BaseEngine.__new__(emas.BaseEngine)
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(
        status_data={
            "OLD/USDT:USDT": {
                "symbol": "OLD/USDT:USDT",
                "pos_side": "SHORT",
                "pnl_usdt": -300.0,
                "total_equity": 5_000.0,
            }
        },
        notify=AsyncMock(),
    )
    engine.db = SimpleNamespace(get_daily_stats=lambda: (0, 0.0))
    engine.get_runtime_common_settings = lambda: {
        "daily_loss_limit": 100_000.0,
        "daily_loss_limit_pct": 5.0,
    }
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(True, set())
    )
    engine._small_account_adaptive_trend_daily_loss_exempt = AsyncMock(
        return_value=False
    )
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))

    assert asyncio.run(engine.check_daily_loss_limit()) is False


def test_daily_loss_keeps_status_fallback_when_position_snapshot_fails():
    emas = _emas_module()
    engine = emas.BaseEngine.__new__(emas.BaseEngine)
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(
        status_data={
            "LIVE/USDT:USDT": {
                "symbol": "LIVE/USDT:USDT",
                "pos_side": "LONG",
                "pnl_usdt": -300.0,
                "total_equity": 5_000.0,
            }
        },
        notify=AsyncMock(),
    )
    engine.db = SimpleNamespace(get_daily_stats=lambda: (0, 0.0))
    engine.get_runtime_common_settings = lambda: {
        "daily_loss_limit": 100_000.0,
        "daily_loss_limit_pct": 5.0,
    }
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(False, set())
    )
    engine._small_account_adaptive_trend_daily_loss_exempt = AsyncMock(
        return_value=False
    )
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))
    engine.exit_position = AsyncMock()

    assert asyncio.run(engine.check_daily_loss_limit()) is True
    engine.exit_position.assert_awaited_once_with(
        "LIVE/USDT:USDT",
        "DailyLossLimit",
    )


def test_daily_loss_leaves_small_account_adaptive_trend_to_exchange_sl():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "SPCX/USDT:USDT"
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(
        status_data={
            symbol: {
                "symbol": symbol,
                "pos_side": "LONG",
                "pnl_usdt": -5.0,
                "total_equity": 80.0,
            }
        },
        notify=AsyncMock(),
    )
    engine.db = SimpleNamespace(get_daily_stats=lambda: (1, 0.0))
    engine.get_runtime_common_settings = lambda: {
        "daily_loss_limit": 100_000.0,
        "daily_loss_limit_pct": 5.0,
    }
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(True, {"SPCX/USDT"})
    )
    engine.get_balance_info = AsyncMock(return_value=(80.0, 20.0, 0.0))
    engine._get_utbreakout_trailing_state = lambda _symbol: {
        "strategy": "adaptive_breakout_trend_v1",
        "small_account_aggressive_active": True,
        "last_stop_price": 143.27,
    }
    engine.exit_position = AsyncMock()
    engine._persist_daily_loss_entry_lock = AsyncMock(
        side_effect=AssertionError(
            "small-account SL-managed position must not create forced-exit lock"
        )
    )

    assert asyncio.run(engine.check_daily_loss_limit()) is False

    engine.exit_position.assert_not_awaited()
    engine._persist_daily_loss_entry_lock.assert_not_awaited()
    engine.ctrl.notify.assert_not_awaited()


def test_small_account_adaptive_trend_mode_can_enter_after_daily_loss():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(status_data={}, notify=AsyncMock())
    engine.db = SimpleNamespace(get_daily_stats=lambda: (8, -70.0))
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": "adaptive_breakout_trend_v1"
    }
    engine._get_utbot_filtered_breakout_config = lambda _params: {}
    engine._adaptive_breakout_trend_runtime_config = lambda _cfg: {
        "small_account_aggressive_enabled": True,
        "small_account_equity_threshold_usdt": 1_000.0,
    }
    engine.get_balance_info = AsyncMock(return_value=(80.0, 80.0, 0.0))
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(True, set())
    )

    assert asyncio.run(engine.check_daily_loss_limit()) is False


def test_flat_small_account_does_not_bypass_when_position_snapshot_fails():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(status_data={}, notify=AsyncMock())
    engine.db = SimpleNamespace(get_daily_stats=lambda: (8, -70.0))
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(False, set())
    )
    engine._small_account_adaptive_trend_daily_loss_exempt = AsyncMock(
        return_value=True
    )
    engine.get_runtime_common_settings = lambda: {
        "daily_loss_limit": 100_000.0,
        "daily_loss_limit_pct": 5.0,
    }
    engine.get_balance_info = AsyncMock(return_value=(80.0, 80.0, 0.0))

    assert asyncio.run(engine.check_daily_loss_limit()) is True


def test_daily_loss_still_forces_other_strategy_positions_closed():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "BTC/USDT:USDT"
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(
        status_data={
            symbol: {
                "symbol": symbol,
                "pos_side": "SHORT",
                "pnl_usdt": -300.0,
                "total_equity": 5_000.0,
            }
        },
        notify=AsyncMock(),
    )
    engine.db = SimpleNamespace(get_daily_stats=lambda: (0, 0.0))
    engine.get_runtime_common_settings = lambda: {
        "daily_loss_limit": 100_000.0,
        "daily_loss_limit_pct": 5.0,
    }
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(True, {"BTC/USDT"})
    )
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))
    engine._get_utbreakout_trailing_state = lambda _symbol: {
        "strategy": "utbot_filtered_breakout_v1",
        "small_account_aggressive_active": False,
    }
    engine._small_account_adaptive_trend_daily_loss_exempt = AsyncMock(
        return_value=False
    )
    engine.exit_position = AsyncMock()
    persisted = []
    engine._persist_daily_loss_entry_lock = AsyncMock(
        side_effect=lambda **kwargs: persisted.append(kwargs)
    )

    assert asyncio.run(engine.check_daily_loss_limit()) is True
    engine.exit_position.assert_awaited_once_with(symbol, "DailyLossLimit")
    assert persisted[0]["forced_exit_symbols"] == [symbol]


def test_daily_loss_forces_exchange_position_missing_from_status_cache():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "ETH/USDT"
    engine.cfg = {"system_settings": {"active_engine": "signal"}}
    engine.ctrl = SimpleNamespace(status_data={}, notify=AsyncMock())
    engine.db = SimpleNamespace(get_daily_stats=lambda: (1, -300.0))
    engine.get_runtime_common_settings = lambda: {
        "daily_loss_limit": 100_000.0,
        "daily_loss_limit_pct": 5.0,
    }
    engine._fetch_active_position_symbols_checked = AsyncMock(
        return_value=(True, {symbol})
    )
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))
    engine._get_utbreakout_trailing_state = lambda _symbol: None
    engine._small_account_adaptive_trend_daily_loss_exempt = AsyncMock(
        return_value=False
    )
    engine.exit_position = AsyncMock()
    persisted = []
    engine._persist_daily_loss_entry_lock = AsyncMock(
        side_effect=lambda **kwargs: persisted.append(kwargs)
    )

    assert asyncio.run(engine.check_daily_loss_limit()) is True
    engine.exit_position.assert_awaited_once_with(symbol, "DailyLossLimit")
    assert persisted[0]["forced_exit_symbols"] == [symbol]


def test_daily_loss_lock_waits_for_inflight_entry_gateway(tmp_path):
    emas = _emas_module()
    from trading_safety.order_state import SQLiteTradingStateStore

    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    store = SQLiteTradingStateStore(tmp_path / "daily-loss-race.sqlite3")
    gateway_lock = asyncio.Lock()
    engine.trading_state_store = store
    engine.ctrl = SimpleNamespace(
        crypto_execution_service=SimpleNamespace(
            state_store=store,
            order_gateway=SimpleNamespace(_global_entry_lock=gateway_lock),
        )
    )

    async def scenario():
        await gateway_lock.acquire()
        persist_task = asyncio.create_task(
            engine._persist_daily_loss_entry_lock(
                total_daily_pnl=-300.0,
                effective_limit=250.0,
                forced_exit_symbols=["BTC/USDT:USDT"],
            )
        )
        await asyncio.sleep(0)
        assert persist_task.done() is False
        assert store.get_runtime_state("daily_loss_entry_lock") is None

        gateway_lock.release()
        payload = await persist_task
        assert payload["forced_exit_symbols"] == ["BTC/USDT:USDT"]
        assert store.entry_block_reason("ETH/USDT:USDT").startswith(
            "DAILY_LOSS_LOCKED:"
        )

    asyncio.run(scenario())


def test_scanner_keeps_symbol_and_protection_when_position_lookup_fails():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "BTC/USDT:USDT"
    engine.running = True
    engine.scanner_active_symbol = symbol
    engine.active_symbols = set()
    engine.last_volume_scan = 0.0
    engine.consecutive_errors = 0
    engine.ctrl = SimpleNamespace(is_paused=True, status_data={})
    engine.db = SimpleNamespace(get_daily_stats=lambda: (0, 0.0))
    engine.get_runtime_trade_config = lambda: {
        "common_settings": {"scanner_enabled": True, "entry_timeframe": "15m"},
        "strategy_params": {"active_strategy": "rspt"},
    }
    engine.get_runtime_common_settings = lambda: {
        "scanner_enabled": True,
        "entry_timeframe": "15m",
        "exit_timeframe": "4h",
        "leverage": 5,
    }
    engine.get_active_position_symbols = AsyncMock(return_value=set())
    engine._cleanup_orphan_protection_orders = AsyncMock(
        return_value={"cancelled": 0, "symbols": {}}
    )
    engine._fetch_server_position_checked = AsyncMock(return_value=(False, None))
    engine._cancel_protection_orders = AsyncMock()
    engine._reconcile_closed_position_protection = AsyncMock()
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))
    engine.is_upbit_mode = lambda: False

    asyncio.run(engine.poll_tick())

    assert engine.scanner_active_symbol == symbol
    engine._cancel_protection_orders.assert_not_awaited()
    engine._reconcile_closed_position_protection.assert_not_awaited()


def test_scanner_flat_completion_accounts_before_clearing_runtime_state():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "DRAM/USDT:USDT"
    state = {"strategy": "adaptive_breakout_trend_v1", "entry_price": 57.23}
    engine._get_utbreakout_trailing_state = lambda _symbol: state
    engine._cancel_protection_orders = AsyncMock(return_value=2)
    engine._reconcile_closed_position_protection = AsyncMock(
        return_value={"cleanup_confirmed": True, "position_active": False}
    )
    engine._record_closed_trade_accounting = AsyncMock(
        return_value={"status": "RECORDED"}
    )
    engine._clear_utbreakout_trailing_state = MagicMock()
    engine._clear_aggressive_growth_position = MagicMock()

    completed = asyncio.run(engine._finalize_scanner_flat_position(symbol))

    assert completed is True
    accounting_state = engine._record_closed_trade_accounting.call_args.kwargs["state"]
    assert accounting_state["_require_exchange_fills"] is True
    engine._clear_utbreakout_trailing_state.assert_called_once_with(
        symbol,
        finalize=True,
        reason="scanner position completed",
    )


def test_scanner_flat_completion_retries_unresolved_accounting_without_clearing():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "DRAM/USDT:USDT"
    engine._get_utbreakout_trailing_state = lambda _symbol: {"entry_price": 57.23}
    engine._cancel_protection_orders = AsyncMock(return_value=0)
    engine._reconcile_closed_position_protection = AsyncMock(
        return_value={"cleanup_confirmed": True, "position_active": False}
    )
    engine._record_closed_trade_accounting = AsyncMock(
        return_value={"status": "UNRESOLVED"}
    )
    engine._clear_utbreakout_trailing_state = MagicMock()

    completed = asyncio.run(engine._finalize_scanner_flat_position(symbol))

    assert completed is False
    engine._clear_utbreakout_trailing_state.assert_not_called()


def test_coin_selector_quietly_skips_dated_futures_contracts():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)

    assert engine._coin_selector_is_dated_futures_symbol(
        "BTC/USDT:USDT260925"
    ) is True
    assert engine._coin_selector_is_dated_futures_symbol(
        "ETH/USDT:USDT-261225"
    ) is True
    assert engine._coin_selector_is_dated_futures_symbol(
        "SNXX/USDT:USDT"
    ) is False


def test_scanner_adopts_restart_position_before_volume_scan():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "UNI/USDT:USDT"
    engine.running = True
    engine.scanner_active_symbol = None
    engine.active_symbols = set()
    engine.last_volume_scan = 0.0
    engine.consecutive_errors = 0
    engine.ctrl = SimpleNamespace(is_paused=True, status_data={})
    engine.db = SimpleNamespace(get_daily_stats=lambda: (0, 0.0))
    engine.get_runtime_trade_config = lambda: {
        "common_settings": {"scanner_enabled": True, "entry_timeframe": "15m"},
        "strategy_params": {"active_strategy": "rspt"},
    }
    engine.get_runtime_common_settings = lambda: {
        "scanner_enabled": True,
        "entry_timeframe": "15m",
        "exit_timeframe": "4h",
        "leverage": 5,
    }
    engine.get_active_position_symbols = AsyncMock(return_value={symbol})
    engine._cleanup_orphan_protection_orders = AsyncMock(
        return_value={"cancelled": 0, "symbols": {}}
    )
    engine._fetch_server_position_checked = AsyncMock(
        return_value=(True, {"symbol": symbol, "side": "short"})
    )
    engine.scan_and_trade_high_volume = AsyncMock()
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))
    engine.is_upbit_mode = lambda: False

    asyncio.run(engine.poll_tick())

    assert engine.scanner_active_symbol == symbol
    engine.scan_and_trade_high_volume.assert_not_awaited()


def test_status_lookup_failure_never_audits_protection_as_flat():
    emas = _emas_module()
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    symbol = "BTC/USDT:USDT"
    engine.position_cache = {}
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 4_000.0, 0.0))
    engine.db = SimpleNamespace(get_daily_stats=lambda: (0, 0.0))
    engine._fetch_server_position_checked = AsyncMock(return_value=(False, None))
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": "cameron",
        "entry_mode": "cross",
    }
    engine.get_runtime_common_settings = lambda: {
        "tp_sl_enabled": True,
        "take_profit_enabled": True,
        "stop_loss_enabled": True,
        "entry_timeframe": "15m",
        "exit_timeframe": "4h",
        "leverage": 5,
    }
    engine.vbo_states = {}
    engine.fisher_states = {}
    engine.last_entry_reason = {}
    engine.last_stateful_diag = {}
    engine.last_utbot_filter_pack_status = {}
    engine.last_utbot_rsi_momentum_filter_status = {}
    engine.last_utsmc_candidate_filter_status = {}
    engine.last_utbot_filtered_breakout_status = {}
    engine.last_protection_order_status = {}
    engine._get_utbot_filter_pack = lambda _params: {}
    engine._get_utbot_rsi_momentum_filter_config = lambda _params: {}
    engine._protection_expected_from_config = lambda _symbol, _pos: (True, True)
    engine._get_utbreakout_trailing_state = lambda _symbol: None
    engine._audit_protection_orders = AsyncMock()
    engine.check_mmr_alert = AsyncMock()
    engine.is_upbit_mode = lambda: False
    engine.get_quote_currency = lambda: "USDT"
    engine.market_data_exchange = SimpleNamespace(id="binance")
    engine.ctrl = SimpleNamespace(
        status_data={},
        market_data_source_label="BINANCE FUTURES",
        get_network_status_label=lambda: "TESTNET",
        get_exchange_display_name=lambda: "BINANCE",
        get_runtime_diag=lambda: {},
    )

    result = asyncio.run(engine.check_status(symbol, 100.0))

    assert result == "UNKNOWN"
    engine._audit_protection_orders.assert_not_awaited()
    protection = engine.ctrl.status_data[symbol]["protection_config"]
    assert protection["audit_status"] == "POSITION_FETCH_FAILED"
    assert protection["missing_sl"] is False
