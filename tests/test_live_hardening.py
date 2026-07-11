import os
import json
import pytest
import shutil
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

# Module Imports
from emas import (
    LiveOrderPlan,
    TakeProfitOrderPlan,
    InvalidOrderPlan,
    build_live_order_plan_from_decision,
    validate_live_order_plan,
    apply_runtime_safety_defaults,
    enforce_activation_stage,
    write_critical_pause_state,
    load_critical_pause_state,
    assert_trading_allowed,
    manual_resume_trading,
    calculate_initial_stop_price,
    calculate_position_qty_by_risk,
    calculate_tp_price,
    build_ladder_tp_orders,
    PAUSE_STATE_FILE,
)
from utbreakout.market_context import build_market_context
from utbreakout.engine_router import TradeDecision, LadderTP

# Clean up pause file before and after tests
@pytest.fixture(autouse=True)
def cleanup_pause_files():
    if os.path.exists(PAUSE_STATE_FILE):
        try:
            os.remove(PAUSE_STATE_FILE)
        except Exception:
            pass
    yield
    if os.path.exists(PAUSE_STATE_FILE):
        try:
            os.remove(PAUSE_STATE_FILE)
        except Exception:
            pass

# 1. Test DMI and HTF defaults in build_market_context
def test_market_context_does_not_invent_favorable_dmi_defaults():
    ctx = build_market_context(
        rows=[],
        idx=0,
        symbol="BTC/USDT",
        values={"utbot_direction": "LONG", "close": 100.0, "high": 101.0, "low": 99.0}
    )
    assert ctx.plus_di == 0.0
    assert ctx.minus_di == 0.0
    assert "MISSING_DMI" in ctx.quality.reasons

def test_market_context_does_not_invent_htf_trend_from_side():
    ctx = build_market_context(
        rows=[],
        idx=0,
        symbol="BTC/USDT",
        values={"utbot_direction": "LONG", "close": 100.0, "high": 101.0, "low": 99.0}
    )
    assert ctx.htf_trend == "FLAT"
    assert "MISSING_HTF_TREND" in ctx.quality.reasons

# 2. Real account equity requirements in order plans
def test_build_order_plan_requires_real_equity():
    context = build_market_context(
        rows=[],
        idx=0,
        symbol="BTC/USDT",
        values={"close": 100.0, "atr": 2.0}
    )
    decision = TradeDecision(
        valid=True,
        side="LONG",
        risk_pct=0.5,
        ladder_tp=None,
        engine="Momentum",
        regime="ACTIVE",
        confidence=0.8,
        expected_r=2.5,
        reasons=[]
    )
    cfg = {"max_risk_per_trade_pct": 1.0}

    with pytest.raises(InvalidOrderPlan) as excinfo:
        build_live_order_plan_from_decision("BTC/USDT", decision, context, cfg, account_equity=None)
    assert "real account equity" in str(excinfo.value)

    with pytest.raises(InvalidOrderPlan) as excinfo:
        build_live_order_plan_from_decision("BTC/USDT", decision, context, cfg, account_equity=-500)
    assert "real account equity" in str(excinfo.value)

# 3. Validation and precision bounds checks
def test_validate_order_plan_limits():
    plan = LiveOrderPlan(
        symbol="BTC/USDT",
        side="LONG",
        entry_type="MARKET",
        entry_price=100.0,
        qty=10.0,
        risk_pct=0.5,
        initial_sl_price=96.0,
        tp_orders=[
            TakeProfitOrderPlan("TP1", "sell", 104.0, 5.0, True, False, 2.0, 50.0),
            TakeProfitOrderPlan("TP2", "sell", 108.0, 5.0, True, False, 4.0, 50.0),
        ],
        ladder=None,
        reduce_only=False,
        engine="Momentum",
        regime="ACTIVE",
        confidence=0.8,
        expected_r=2.5,
        reasons=[]
    )
    cfg = {"max_risk_per_trade_pct": 1.0}
    assert validate_live_order_plan(plan, cfg) is True

    # Bad SL
    plan.initial_sl_price = -1.0
    with pytest.raises(InvalidOrderPlan):
        validate_live_order_plan(plan, cfg)
    plan.initial_sl_price = 96.0

    # Over sized risk
    plan.risk_pct = 2.0
    with pytest.raises(InvalidOrderPlan):
        validate_live_order_plan(plan, cfg)

# 4. Critical Pause Lock and persistent restart checks
def test_assert_trading_allowed_blocks_after_persisted_pause():
    assert_trading_allowed("BTC/USDT") # should pass

    write_critical_pause_state("BTC/USDT", "SL placement failed", RuntimeError("Order error"))

    with pytest.raises(RuntimeError) as excinfo:
        assert_trading_allowed("BTC/USDT")
    assert "TRADING_CRITICAL_PAUSED" in str(excinfo.value)

def test_manual_resume_archives_pause_file():
    write_critical_pause_state("BTC/USDT", "SL placement failed", RuntimeError("Order error"))
    assert os.path.exists(PAUSE_STATE_FILE) is True

    with pytest.raises(ValueError):
        manual_resume_trading("BTC/USDT", "WRONG_TOKEN")

    res = manual_resume_trading("BTC/USDT", "I_CONFIRM_MANUAL_RISK_CHECK_DONE")
    assert res["status"] == "RESUME_REQUESTED"
    assert os.path.exists(PAUSE_STATE_FILE) is True

# 5. Testing stages and settings defaults
def test_safety_stage_enforcement():
    cfg = {"mode": "live", "live_trading": True, "live_activation_stage": "SMALL_LIVE_025"}

    # Missing opt-in on real live should raise ValueError
    with pytest.raises(ValueError):
        enforce_activation_stage(cfg)

    cfg["advanced_alpha_live_opt_in"] = True
    enforced = enforce_activation_stage(cfg)
    assert enforced["advanced_alpha_engine_enabled"] is True
    assert enforced["max_risk_per_trade_pct"] <= 0.25

    # Paper stage enforces settings without opt-in
    cfg_paper = {"mode": "paper", "live_activation_stage": "PAPER_ONLY"}
    enforced_paper = enforce_activation_stage(cfg_paper)
    assert enforced_paper["advanced_alpha_engine_enabled"] is True
    assert enforced_paper["live_trading"] is False
