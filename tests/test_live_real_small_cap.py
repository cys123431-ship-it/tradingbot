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
        self.dual_side_position = False
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

    def fapiPrivateGetPositionSideDual(self):
        return {"dualSidePosition": self.dual_side_position}

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
    assert enforced["max_real_position_notional_usdt"] == pytest.approx(5.58)
    assert enforced["max_real_loss_per_trade_usdt"] == pytest.approx(0.31)
    assert enforced["max_risk_per_trade_pct"] == pytest.approx(10.0)
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
    assert cfg["max_real_position_notional_usdt"] == pytest.approx(5.58)
    assert cfg["max_real_loss_per_trade_usdt"] == pytest.approx(0.31)
    assert cfg["max_daily_real_loss_usdt"] == pytest.approx(2.232)
    assert cfg["max_weekly_real_loss_usdt"] == pytest.approx(11.16)
    assert cfg["max_open_positions"] == 1
    assert cfg["max_same_direction_positions"] == 1
    assert cfg["live_real_effective_limits"]["max_position_notional_pct"] == pytest.approx(0.09)


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
    assert result["live_real_limits"]["max_position_notional_usdt"] == pytest.approx(5.58)

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


def test_preflight_blocks_binance_hedge_mode(monkeypatch):
    monkeypatch.setattr(emas, "load_critical_pause_state", lambda: None)
    monkeypatch.setattr(emas, "load_live_real_risk_state", lambda: {"daily_realized_pnl_usdt": 0, "weekly_realized_pnl_usdt": 0})

    exchange = FakeExchange()
    exchange.dual_side_position = True
    bot = make_fake_bot(exchange)

    with pytest.raises(emas.TradingSafetyError, match="hedge mode"):
        asyncio.run(bot.preflight_live_real_check("BTC/USDT:USDT", live_cfg()))


def test_live_real_order_caps_scale_qty_and_tp_quantities():
    cfg = emas.enforce_activation_stage(live_cfg())
    plan = make_plan(qty=1.0, entry=100.0, sl=95.0)
    context = SimpleNamespace(close=100.0)

    capped = emas.enforce_live_real_order_caps(plan, context, cfg)

    assert capped.qty <= 0.055856
    assert sum(tp.qty for tp in capped.tp_orders) <= capped.qty * 1.0001
    assert emas.validate_live_real_order_caps_after_rounding(capped, cfg) is True


def test_live_real_validation_rejects_rounding_and_min_notional_over_cap():
    cfg = emas.enforce_activation_stage(live_cfg())
    with pytest.raises(emas.InvalidOrderPlan):
        emas.validate_live_real_order_caps_after_rounding(make_plan(qty=0.06, entry=100.0, sl=95.0), cfg)

    with pytest.raises(emas.InvalidOrderPlan):
        emas.validate_min_notional_against_live_cap(6.0, cfg)


def test_live_real_loss_limits_block_daily_and_weekly(monkeypatch):
    cfg = emas.enforce_activation_stage(live_cfg())

    monkeypatch.setattr(emas, "load_live_real_risk_state", lambda: {"daily_realized_pnl_usdt": -2.232, "weekly_realized_pnl_usdt": 0})
    with pytest.raises(emas.TradingPausedError):
        emas.assert_live_real_loss_limits_not_exceeded(cfg)

    monkeypatch.setattr(emas, "load_live_real_risk_state", lambda: {"daily_realized_pnl_usdt": 0, "weekly_realized_pnl_usdt": -11.16})
    with pytest.raises(emas.TradingPausedError):
        emas.assert_live_real_loss_limits_not_exceeded(cfg)


def test_live_real_risk_pct_change_limit_counts_only_increases():
    cfg = emas.enforce_activation_stage(live_cfg())
    state = {
        "risk_pct_change_date_kst": emas._live_real_kst_date_key(cfg),
        "risk_pct_increases_today": 1,
    }

    first = emas.apply_live_real_risk_pct_change(state, 0.005, 0.01, cfg)
    assert first["allowed"] is True
    assert first["state"]["risk_pct_increases_today"] == 2

    second = emas.apply_live_real_risk_pct_change(first["state"], 0.01, 0.02, cfg)
    assert second["allowed"] is False
    assert second["reason"] == "daily_risk_increase_limit"

    lowered = emas.apply_live_real_risk_pct_change(first["state"], 0.01, 0.001, cfg)
    assert lowered["allowed"] is True
    assert lowered["state"]["risk_pct_increases_today"] == 2


def test_live_real_risk_input_safe_and_bounds():
    cfg = emas.enforce_activation_stage(live_cfg())

    assert emas.parse_live_real_risk_pct_input("safe", cfg) == pytest.approx(0.001)
    assert emas.parse_live_real_risk_pct_input("0.5", cfg) == pytest.approx(0.005)
    assert emas.parse_live_real_risk_pct_input("5", cfg) == pytest.approx(0.05)
    assert emas.parse_live_real_risk_pct_input("10", cfg) == pytest.approx(0.10)
    with pytest.raises(ValueError):
        emas.parse_live_real_risk_pct_input("10.1", cfg)


def test_ten_percent_risk_scales_account_proportional_position_cap():
    limits = emas.resolve_live_small_cap_limits(
        {
            "live_real_risk_pct_user": 0.10,
            "default_real_risk_pct": 0.005,
            "max_real_risk_pct": 0.10,
            "max_real_position_notional_pct_of_equity": 0.09,
            "scale_notional_cap_with_risk_pct": True,
            "max_position_notional_pct_hard_limit": 1.0,
        },
        account_equity=100.0,
    )

    assert limits["risk_pct"] == pytest.approx(10.0)
    assert limits["max_loss_per_trade_usdt"] == pytest.approx(10.0)
    assert limits["max_position_notional_pct"] == pytest.approx(1.0)
    assert limits["max_position_notional_usdt"] == pytest.approx(100.0)
    assert limits["notional_scaled_by_risk"] is True


def test_legacy_five_percent_live_cap_is_upgraded_without_changing_selected_risk():
    cfg = live_cfg(
        max_real_risk_pct=0.05,
        scale_notional_cap_with_risk_pct=False,
        max_position_notional_pct_hard_limit=0.50,
        live_real_risk_pct_user=0.02,
    )

    enforced = emas.enforce_activation_stage(cfg)

    assert enforced["max_real_risk_pct"] == pytest.approx(0.10)
    assert enforced["scale_notional_cap_with_risk_pct"] is True
    assert enforced["max_position_notional_pct_hard_limit"] == pytest.approx(1.0)
    assert enforced["live_real_risk_pct_user"] == pytest.approx(0.02)


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
    assert captured["plan"].qty * 100.0 <= 5.58558
    assert captured["plan"].qty * abs(100.0 - captured["plan"].initial_sl_price) <= 0.31031


def test_live_real_absolute_caps_only_tighten_percentage_limits():
    limits = emas.resolve_live_small_cap_limits(
        {
            "account_reference_equity_usdt": 62.0,
            "live_real_risk_pct_user": 0.005,
            "max_real_position_notional_pct_of_equity": 0.09,
            "live_real_absolute_max_notional_usdt": 4.0,
            "live_real_absolute_max_loss_usdt": 0.2,
        }
    )
    assert limits["percentage_max_position_notional_usdt"] == pytest.approx(5.58)
    assert limits["percentage_max_loss_per_trade_usdt"] == pytest.approx(0.31)
    assert limits["max_position_notional_usdt"] == pytest.approx(4.0)
    assert limits["max_loss_per_trade_usdt"] == pytest.approx(0.2)


def test_live_real_period_keys_use_same_kst_boundary_as_risk_changes():
    from datetime import datetime, timezone

    before_kst_midnight = datetime(2026, 1, 1, 14, 59, tzinfo=timezone.utc)
    after_kst_midnight = datetime(2026, 1, 1, 15, 1, tzinfo=timezone.utc)

    assert emas._live_real_period_keys(before_kst_midnight)[0] == "2026-01-01"
    assert emas._live_real_period_keys(after_kst_midnight)[0] == "2026-01-02"
    assert emas._live_real_kst_date_key(now=after_kst_midnight) == "2026-01-02"


def test_live_real_risk_state_missing_file_is_clean_first_run(tmp_path, monkeypatch):
    state_path = tmp_path / "risk.json"
    monkeypatch.setattr(emas, "LIVE_REAL_RISK_STATE_FILE", str(state_path))

    state = emas.load_live_real_risk_state()

    assert state["daily_realized_pnl_usdt"] == 0.0
    assert state["weekly_realized_pnl_usdt"] == 0.0
    assert state["loss_period_timezone"] == "Asia/Seoul"


def test_live_real_risk_state_corruption_fails_closed(tmp_path, monkeypatch):
    state_path = tmp_path / "risk.json"
    state_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(emas, "LIVE_REAL_RISK_STATE_FILE", str(state_path))

    with pytest.raises(emas.LiveRealRiskStateUnreadable, match="LIVE_REAL_RISK_STATE_UNREADABLE"):
        emas.load_live_real_risk_state()

    diagnostic = emas.load_live_real_risk_state(fail_closed=False)
    assert diagnostic["trading_blocked"] is True
    assert diagnostic["reason_code"] == "LIVE_REAL_RISK_STATE_UNREADABLE"


def test_live_real_risk_state_non_object_and_bad_number_fail_closed(tmp_path, monkeypatch):
    state_path = tmp_path / "risk.json"
    monkeypatch.setattr(emas, "LIVE_REAL_RISK_STATE_FILE", str(state_path))

    state_path.write_text("[]", encoding="utf-8")
    with pytest.raises(emas.LiveRealRiskStateUnreadable):
        emas.load_live_real_risk_state()

    state_path.write_text('{"daily_realized_pnl_usdt": "nan"}', encoding="utf-8")
    with pytest.raises(emas.LiveRealRiskStateUnreadable):
        emas.load_live_real_risk_state()


def test_corrupt_risk_state_is_not_overwritten_by_realized_pnl(tmp_path, monkeypatch):
    state_path = tmp_path / "risk.json"
    original = "{broken"
    state_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(emas, "LIVE_REAL_RISK_STATE_FILE", str(state_path))

    with pytest.raises(emas.LiveRealRiskStateUnreadable):
        emas.record_bot_realized_pnl(-1.0)

    assert state_path.read_text(encoding="utf-8") == original
