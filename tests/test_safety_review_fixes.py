import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    engine.get_active_position_symbols = AsyncMock(return_value=set())
    engine.get_balance_info = AsyncMock(return_value=(5_000.0, 5_000.0, 0.0))

    assert asyncio.run(engine.check_daily_loss_limit()) is True
    engine.db.get_daily_stats = lambda: (1, -200.0)
    assert asyncio.run(engine.check_daily_loss_limit()) is False


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
