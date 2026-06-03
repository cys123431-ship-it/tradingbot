import asyncio
from types import SimpleNamespace

import pytest

import emas


def live_cfg(**overrides):
    cfg = {
        "mode": "live",
        "live_activation_stage": "LIVE_REAL_SMALL_CAP",
        "real_live_confirm": emas.LIVE_REAL_CONFIRM_TEXT,
        "max_risk_per_trade_pct": 5.0,
        "max_real_position_notional_usdt": 50.0,
        "max_real_loss_per_trade_usdt": 5.0,
        "max_leverage": 20,
        "leverage": 20,
        "testnet": False,
    }
    cfg.update(overrides)
    return cfg


class FakeExchange:
    def __init__(self):
        self.options = {}
        self.sandbox_calls = []
        self.positions = []
        self.open_orders = []
        self.balance = {"USDT": {"total": 62.0, "free": 62.0}, "info": {"totalWalletBalance": "62"}}
        self.markets = {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "limits": {"cost": {"min": 5.0}},
                "precision": {"amount": 6, "price": 2},
            }
        }

    def set_sandbox_mode(self, enabled):
        self.sandbox_calls.append(bool(enabled))

    def load_markets(self):
        return self.markets

    def market(self, symbol):
        return self.markets.get(symbol, {"symbol": symbol, "limits": {"cost": {"min": 5.0}}})

    def fetch_positions(self):
        return self.positions

    def fetch_open_orders(self, symbol=None):
        return self.open_orders

    def fetch_balance(self):
        return self.balance

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.6f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"


def make_fake_bot(exchange=None):
    bot = emas.MainController.__new__(emas.MainController)
    bot.exchange = exchange or FakeExchange()
    bot.ctrl = SimpleNamespace(is_paused=False)
    bot.ctrl.notify = lambda message: None
    bot.position_cache = None
    bot.position_cache_time = 0
    bot.utbreakout_trailing_states = {}
    bot.is_upbit_mode = lambda: False
    return bot


def make_plan(qty=1.0, entry=100.0, sl=95.0):
    return emas.LiveOrderPlan(
        symbol="BTC/USDT:USDT",
        side="LONG",
        entry_type="MARKET",
        entry_price=entry,
        qty=qty,
        risk_pct=0.5,
        initial_sl_price=sl,
        tp_orders=[
            emas.TakeProfitOrderPlan("TP1", "sell", 105.0, qty * 0.5, True, False, 1.0, 50.0),
            emas.TakeProfitOrderPlan("TP2", "sell", 110.0, qty * 0.5, True, False, 2.0, 50.0),
        ],
        ladder=None,
        reduce_only=False,
        engine="TREND",
        regime="ACTIVE",
        confidence=0.8,
        expected_r=2.0,
        reasons=[],
    )


def test_live_real_stage_requires_exact_confirmation_and_clamps_caps():
    with pytest.raises(ValueError):
        emas.enforce_activation_stage({"mode": "live", "live_activation_stage": "LIVE_REAL_SMALL_CAP"})

    enforced = emas.enforce_activation_stage(live_cfg())

    assert enforced["live_trading"] is True
    assert enforced["real_order_enabled"] is True
    assert enforced["testnet"] is False
    assert enforced["max_leverage"] <= 5
    assert enforced["leverage"] <= 5
    assert enforced["max_real_position_notional_usdt"] <= 15.0
    assert enforced["max_real_loss_per_trade_usdt"] <= 3.0
    assert enforced["max_risk_per_trade_pct"] <= 0.50
    assert enforced["max_open_positions"] == 1


def test_live_real_small_cap_updated_user_requested_limits():
    cfg = emas.enforce_activation_stage({
        "live_activation_stage": "LIVE_REAL_SMALL_CAP",
        "real_live_confirm": "I_CONFIRM_REAL_BINANCE_FUTURES_TRADING",
        "max_leverage": 50,
        "max_real_position_notional_usdt": 1000.0,
        "max_real_loss_per_trade_usdt": 100.0,
        "max_daily_real_loss_usdt": 100.0,
        "max_weekly_real_loss_usdt": 100.0,
    })

    assert cfg["testnet"] is False
    assert cfg["live_trading"] is True
    assert cfg["real_order_enabled"] is True

    assert cfg["max_leverage"] == 5
    assert cfg["max_real_position_notional_usdt"] == 15.0
    assert cfg["max_real_loss_per_trade_usdt"] == 3.0
    assert cfg["max_daily_real_loss_usdt"] == 6.0
    assert cfg["max_weekly_real_loss_usdt"] == 30.0
    assert cfg["max_open_positions"] == 1
    assert cfg["max_same_direction_positions"] == 1


def test_disabled_paper_and_testnet_stage_flags_remain_safe():
    disabled = emas.enforce_activation_stage({"mode": "live", "live_activation_stage": "DISABLED"})
    assert disabled["real_order_enabled"] is False
    assert disabled["live_trading"] is False

    paper = emas.enforce_activation_stage({"mode": "paper", "live_activation_stage": "PAPER_ONLY"})
    assert paper["real_order_enabled"] is False
    assert paper["live_trading"] is False

    testnet = emas.enforce_activation_stage({"mode": "testnet", "live_activation_stage": "TESTNET_ONLY", "leverage": 20})
    assert testnet["real_order_enabled"] is True
    assert testnet["testnet"] is True
    assert testnet["leverage"] <= 2


def test_binance_futures_environment_rejects_real_stage_testnet():
    exchange = FakeExchange()

    with pytest.raises(emas.TradingSafetyError):
        emas.configure_binance_futures_environment(
            exchange,
            {"live_activation_stage": "LIVE_REAL_SMALL_CAP", "testnet": True},
        )

    result = emas.configure_binance_futures_environment(
        exchange,
        {"live_activation_stage": "LIVE_REAL_SMALL_CAP", "testnet": False},
    )
    assert result["defaultType"] == "future"
    assert result["sandbox"] is False
    assert exchange.sandbox_calls[-1] is False


def test_live_real_allowlist_and_tradfi_blocks():
    cfg = emas.enforce_activation_stage(live_cfg())
    assert emas.assert_symbol_allowed_for_live_real("BTC/USDT:USDT", cfg) is True

    with pytest.raises(emas.TradingSafetyError):
        emas.assert_symbol_allowed_for_live_real("SOL/USDT:USDT", cfg)

    cfg_stock = emas.enforce_activation_stage(live_cfg(live_real_allowlist=["TSLA/USDT:USDT"]))
    with pytest.raises(emas.TradingSafetyError):
        emas.assert_symbol_allowed_for_live_real("TSLA/USDT:USDT", cfg_stock)


def test_preflight_blocks_pause_positions_orders_and_passes_clean(monkeypatch):
    monkeypatch.setattr(emas, "load_critical_pause_state", lambda: None)
    monkeypatch.setattr(emas, "load_live_real_risk_state", lambda: {"daily_realized_pnl_usdt": 0, "weekly_realized_pnl_usdt": 0})

    bot = make_fake_bot()
    result = asyncio.run(bot.preflight_live_real_check("BTC/USDT:USDT", live_cfg()))
    assert result["status"] == "OK"
    assert result["account_equity"] == 62.0

    monkeypatch.setattr(emas, "load_critical_pause_state", lambda: {"status": "CRITICAL_PAUSED", "reason": "SL failed"})
    with pytest.raises(emas.TradingPausedError):
        asyncio.run(bot.preflight_live_real_check("BTC/USDT:USDT", live_cfg()))

    monkeypatch.setattr(emas, "load_critical_pause_state", lambda: None)
    positioned = make_fake_bot()
    positioned.exchange.positions = [{"symbol": "BTC/USDT:USDT", "contracts": 0.01, "side": "long"}]
    with pytest.raises(emas.TradingSafetyError):
        asyncio.run(positioned.preflight_live_real_check("BTC/USDT:USDT", live_cfg()))

    ordered = make_fake_bot()
    ordered.exchange.open_orders = [{"id": "open"}]
    with pytest.raises(emas.TradingSafetyError):
        asyncio.run(ordered.preflight_live_real_check("BTC/USDT:USDT", live_cfg()))


def test_live_real_order_caps_scale_qty_and_tp_quantities():
    cfg = emas.enforce_activation_stage(live_cfg())
    plan = make_plan(qty=1.0, entry=100.0, sl=95.0)
    context = SimpleNamespace(close=100.0)

    capped = emas.enforce_live_real_order_caps(plan, context, cfg)

    assert capped.qty <= 0.150151
    assert sum(tp.qty for tp in capped.tp_orders) <= capped.qty * 1.0001
    assert emas.validate_live_real_order_caps_after_rounding(capped, cfg) is True


def test_live_real_validation_rejects_rounding_and_min_notional_over_cap():
    cfg = emas.enforce_activation_stage(live_cfg())
    with pytest.raises(emas.InvalidOrderPlan):
        emas.validate_live_real_order_caps_after_rounding(make_plan(qty=0.16, entry=100.0, sl=95.0), cfg)

    with pytest.raises(emas.InvalidOrderPlan):
        emas.validate_min_notional_against_live_cap(16.0, cfg)


def test_live_real_loss_limits_block_daily_and_weekly(monkeypatch):
    cfg = emas.enforce_activation_stage(live_cfg())

    monkeypatch.setattr(emas, "load_live_real_risk_state", lambda: {"daily_realized_pnl_usdt": -6.0, "weekly_realized_pnl_usdt": 0})
    with pytest.raises(emas.TradingPausedError):
        emas.assert_live_real_loss_limits_not_exceeded(cfg)

    monkeypatch.setattr(emas, "load_live_real_risk_state", lambda: {"daily_realized_pnl_usdt": 0, "weekly_realized_pnl_usdt": -30.0})
    with pytest.raises(emas.TradingPausedError):
        emas.assert_live_real_loss_limits_not_exceeded(cfg)


def test_run_live_real_once_requires_stage(monkeypatch):
    bot = make_fake_bot()
    with pytest.raises(emas.TradingSafetyError):
        asyncio.run(bot.run_live_real_once("BTC/USDT:USDT", {"live_activation_stage": "PAPER_ONLY"}))


def test_run_live_real_once_returns_no_trade_without_execution(monkeypatch):
    bot = make_fake_bot()
    cfg = live_cfg()

    async def preflight(symbol, cfg):
        return {"status": "OK", "account_equity": 62.0}

    async def context(symbol, cfg):
        return SimpleNamespace(close=100.0, atr=2.0, current_candle={})

    async def execute(plan, cfg):
        raise AssertionError("execute_live_order_plan should not run for invalid decisions")

    bot.preflight_live_real_check = preflight
    bot.build_live_context_for_symbol = context
    bot.execute_live_order_plan = execute
    monkeypatch.setattr(emas, "evaluate_final_trade_decision", lambda **kwargs: emas.TradeDecision.none("NO_EDGE"))

    result = asyncio.run(bot.run_live_real_once("BTC/USDT:USDT", cfg))
    assert result["status"] == "NO_TRADE"
    assert result["reason"] == ["NO_EDGE"]


def test_run_live_real_once_builds_capped_plan_and_executes(monkeypatch):
    bot = make_fake_bot()
    cfg = live_cfg()
    captured = {}

    async def preflight(symbol, cfg):
        return {"status": "OK", "account_equity": 62.0}

    async def context(symbol, cfg):
        return SimpleNamespace(close=100.0, atr=2.0, current_candle={})

    async def execute(plan, cfg):
        captured["plan"] = plan
        return {"status": "LIVE_ORDER_PLAN_EXECUTED", "plan": plan}

    decision = emas.TradeDecision(
        valid=True,
        side="LONG",
        engine="TREND_CONTINUATION_LONG",
        risk_pct=2.0,
        ladder_tp=None,
        regime="ACTIVE",
        confidence=0.8,
        expected_r=2.0,
        reasons=["TEST"],
    )

    bot.preflight_live_real_check = preflight
    bot.build_live_context_for_symbol = context
    bot.execute_live_order_plan = execute
    monkeypatch.setattr(emas, "evaluate_final_trade_decision", lambda **kwargs: decision)

    result = asyncio.run(bot.run_live_real_once("BTC/USDT:USDT", cfg))
    assert result["status"] == "LIVE_ORDER_PLAN_EXECUTED"
    assert captured["plan"].qty * 100.0 <= 15.015
    assert captured["plan"].qty * abs(100.0 - captured["plan"].initial_sl_price) <= 3.003
