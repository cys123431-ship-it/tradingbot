"""EMA200 small-account strategy revision 2.

Exit on the entry timeframe's UT, ATR stop on every entry with fixed equity
risk sizing, a wider profit-stop staircase and a post-exit re-entry cooldown.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler

import emas
from bot_runtime.database import DBManager
from bot_runtime.ema200_profit_stop import ema200_profit_stop_target
from bot_runtime.ema200_utbot_rsi import (
    EMA200_STRATEGY_REVISION,
    EMA200_UTBOT_RSI_STRATEGY,
    build_ema200_utbot_rsi_risk_plan,
    calculate_ema200_utbot_rsi_emergency_stop_price,
    ema200_atr_stop_percent,
    ema200_effective_exit_timeframe,
    ema200_exit_timeframe_label,
    ema200_reentry_allowed_at_ms,
    normalize_ema200_utbot_rsi_config,
)
from tests.test_ema200_utbot_rsi_strategy import (
    _TelegramQuery,
    _registered_telegram_controller,
)
from tests.test_utbreakout_sl_lockout import _build_engine


def _rows(n, high, low, close=100.0):
    return [[i, close, high, low, close, 1.0] for i in range(n)]


def test_atr_stop_percent_uses_completed_wilder_atr_and_clamps():
    # Constant true range 2.0 on a 100 price: ATR 2 -> 2x = 4%.
    rows = _rows(40, 101.0, 99.0)
    assert ema200_atr_stop_percent(rows, entry_price=100.0, max_percent=5.0) == pytest.approx(4.0)
    assert ema200_atr_stop_percent(rows, entry_price=100.0, max_percent=3.0) == pytest.approx(3.0)
    tight = _rows(40, 100.1, 99.9)
    assert ema200_atr_stop_percent(tight, entry_price=100.0, min_percent=1.0) == pytest.approx(1.0)
    assert ema200_atr_stop_percent(_rows(10, 101, 99), entry_price=100.0) is None


def test_risk_plan_sizes_to_fixed_equity_loss_and_always_requires_stop():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=90.0, free_balance=90.0, entry_price=100.0,
        config={}, consecutive_losses=0, stop_percent=3.0,
    )
    assert plan["sizing_mode"] == "small_account_atr_risk"
    assert plan["emergency_stop_required"] is True
    assert plan["strategy_exit_only"] is False
    assert plan["planned_notional"] == pytest.approx(45.0)  # 90*1.5% / 3%
    assert plan["planned_emergency_loss_usdt"] == pytest.approx(1.35)
    assert plan["emergency_exit_percent"] == pytest.approx(3.0)

    # The loss ladder still caps size after a losing streak (10% margin x 5x).
    capped = build_ema200_utbot_rsi_risk_plan(
        account_equity=90.0, free_balance=90.0, entry_price=100.0,
        config={}, consecutive_losses=4, stop_percent=1.0,
    )
    assert capped["planned_notional"] == pytest.approx(45.0)
    assert capped["margin_cap_applied"] is True
    assert capped["planned_emergency_loss_usdt"] == pytest.approx(0.45)

    stop = calculate_ema200_utbot_rsi_emergency_stop_price(
        side="short", entry_price=100.0, config={}, stop_percent=3.0,
    )
    assert stop == pytest.approx(103.0)


def test_risk_plan_without_atr_keeps_the_legacy_ladder():
    plan = build_ema200_utbot_rsi_risk_plan(
        account_equity=90.0, free_balance=90.0, entry_price=100.0,
        config={}, consecutive_losses=0,
    )
    assert plan["sizing_mode"] == "small_account_loss_ladder"


@pytest.mark.parametrize("side,mark,expected", [
    ("long", 102.9, None),             # ROI 14.5% <= start 15%
    ("long", 103.02, (10.0, 102.0)),   # ROI 15.1% locks 10%
    ("long", 104.1, (15.0, 103.0)),    # ROI 20.5% locks 15%
    ("short", 96.98, (10.0, 98.0)),    # symmetric
])
def test_profit_stop_trails_one_step_below_after_start(side, mark, expected):
    target = ema200_profit_stop_target(side, 100.0, mark, 5, step_percent=5.0, start_percent=15.0)
    if expected is None:
        assert target is None
    else:
        _, locked, price = target
        assert (locked, price) == (pytest.approx(expected[0]), pytest.approx(expected[1]))


def test_profit_stop_legacy_semantics_unchanged_without_start():
    _, locked, price = ema200_profit_stop_target("long", 100.0, 101.2, 5)
    assert (locked, price) == (pytest.approx(5.0), pytest.approx(101.0))


def test_exit_defaults_to_entry_timeframe_and_legacy_choices_remain():
    cfg = normalize_ema200_utbot_rsi_config({"timeframe": "4h"})
    assert cfg["exit_timeframe"] == "entry"
    assert ema200_effective_exit_timeframe(cfg) == "4h"
    assert ema200_exit_timeframe_label(cfg) == "4h(진입봉)"
    legacy = normalize_ema200_utbot_rsi_config({"timeframe": "4h", "exit_timeframe": "15m"})
    assert ema200_effective_exit_timeframe(legacy) == "15m"


def test_reentry_waits_for_the_next_completed_entry_candle():
    exit_time = datetime(2026, 10, 1, 10, 37, tzinfo=timezone.utc).isoformat()
    allowed = ema200_reentry_allowed_at_ms(exit_time, "4h", 1)
    assert datetime.fromtimestamp(allowed / 1000, tz=timezone.utc) == datetime(
        2026, 10, 1, 12, 0, tzinfo=timezone.utc
    )
    assert ema200_reentry_allowed_at_ms(exit_time, "4h", 0) is None


def _cooldown_engine(tmp_path, cooldown):
    engine = _build_engine(tmp_path)
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": EMA200_UTBOT_RSI_STRATEGY,
        "EMA200UTBotRSI2H": {"timeframe": "4h", "reentry_cooldown_candles": cooldown},
    }

    async def allowed(*_args, **_kwargs):
        return True, ""

    engine._ema200_entry_volume_allowed = allowed
    engine._ema200_exit_timeframe_aligned = allowed
    engine.db = DBManager(str(tmp_path / "trades.db"))
    engine.db.log_trade_entry("ETH/USDT:USDT", "long", 100.0, 1.0, strategy=EMA200_UTBOT_RSI_STRATEGY)
    engine.db.log_trade_close("ETH/USDT:USDT", -1.0, -1.0, 99.0, "test")
    reached = []

    def next_gate(symbol):
        reached.append(symbol)
        return True, "stop after cooldown gate"

    engine._is_automatic_daily_symbol_entry_locked = next_gate
    return engine, reached


def test_entry_is_blocked_until_the_cooldown_candle_closes(tmp_path):
    engine, reached = _cooldown_engine(tmp_path, cooldown=1)

    asyncio.run(engine.entry("SOL/USDT:USDT", "long", 100.0))

    assert reached == []
    assert engine.last_entry_reason["SOL/USDT:USDT"].startswith("EMA200_REENTRY_COOLDOWN")
    engine.db.conn.close()


def test_cooldown_zero_allows_the_next_gate(tmp_path):
    engine, reached = _cooldown_engine(tmp_path, cooldown=0)

    asyncio.run(engine.entry("SOL/USDT:USDT", "long", 100.0))

    assert reached == ["SOL/USDT:USDT"]
    engine.db.conn.close()


def test_entry_passes_the_atr_stop_through_every_stop_calculation():
    import inspect

    source = inspect.getsource(emas.SignalEntryMixin.entry)
    assert "stop_percent=ema200_stop_percent" in source
    assert source.count("stop_percent=ema200_risk_plan.get('emergency_exit_percent')") == 2
    assert "ema200_risk_plan.get('emergency_exit_percent')" in source
    assert "gate='atr_stop_unavailable'" in source


class _MigrationConfig(dict):
    def __init__(self, raw):
        super().__init__({"binance_futures": {"strategy_params": {"EMA200UTBotRSI2H": raw}}})
        self.updates = []

    async def update_value(self, path, value):
        self.updates.append((path[-1], value))
        self["binance_futures"]["strategy_params"]["EMA200UTBotRSI2H"][path[-1]] = value


def _migration_controller(raw):
    controller = _registered_telegram_controller()
    controller.cfg = _MigrationConfig(dict(raw))
    notices = []

    async def notify(text):
        notices.append(text)

    controller.notify = notify
    return controller, notices


def test_revision_migration_moves_legacy_settings_once():
    controller, notices = _migration_controller({"timeframe": "1h", "exit_timeframe": "15m"})

    changes = asyncio.run(controller._apply_ema200_strategy_revision())

    assert changes == {"exit_timeframe": "entry", "timeframe": "4h"}
    assert ("strategy_revision", EMA200_STRATEGY_REVISION) in controller.cfg.updates
    assert notices and "revision 2" in notices[0]
    assert asyncio.run(controller._apply_ema200_strategy_revision()) is None


def test_revision_migration_keeps_an_already_long_entry_timeframe():
    controller, _ = _migration_controller({"timeframe": "6h", "exit_timeframe": "30m"})

    changes = asyncio.run(controller._apply_ema200_strategy_revision())

    assert changes == {"exit_timeframe": "entry"}


def test_telegram_small_account_risk_and_entry_exit_buttons():
    controller = _registered_telegram_controller()
    buttons = {
        button.callback_data: button.text
        for row in controller._build_ema200_utbot_rsi_keyboard().inline_keyboard
        for button in row
    }
    assert {"e2h:srisk:1", "e2h:srisk:1.5", "e2h:srisk:2"} <= set(buttons)
    assert buttons["e2h:srisk:1.5"].startswith("✅")
    assert buttons["e2h:exit_tf:entry"].startswith("✅")

    handler = next(handler for handler, _ in controller.tg_app.handlers
                   if isinstance(handler, CallbackQueryHandler))
    query = _TelegramQuery("e2h:srisk:2")
    asyncio.run(handler.callback(SimpleNamespace(callback_query=query), None))
    assert controller.cfg.updates[-1] == (
        ["binance_futures", "strategy_params", "EMA200UTBotRSI2H", "small_account_risk_percent"], 2.0
    )
    bad = _TelegramQuery("e2h:srisk:9")
    asyncio.run(handler.callback(SimpleNamespace(callback_query=bad), None))
    assert controller.cfg.updates[-1][1] == 2.0
