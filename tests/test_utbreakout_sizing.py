import pytest

from utbreakout.sizing import (
    build_aggressive_growth_overlay_plan,
    build_aggressive_growth_pyramid_plan,
    build_position_risk_multiplier,
    calculate_adaptive_risk_pct,
)


def test_position_sizing_reduces_risk_in_high_volatility():
    result = build_position_risk_multiplier({
        "atr_pct": 3.0,
        "meta_probability": 0.70,
    })

    assert result["components"]["volatility"] < 0.5
    assert result["risk_multiplier"] < 0.5


def test_position_sizing_reduces_risk_during_drawdown():
    result = build_position_risk_multiplier({
        "atr_pct": 1.0,
        "meta_probability": 0.70,
        "drawdown_pct": 10.0,
    })

    assert result["components"]["drawdown"] < 1.0
    assert result["risk_multiplier"] < 1.0


def test_position_sizing_blocks_after_hard_loss_streak():
    result = build_position_risk_multiplier({
        "meta_probability": 0.70,
        "consecutive_losses": 5,
    })

    assert result["blocked"] is True
    assert result["risk_multiplier"] == 0.0


def test_position_sizing_keeps_good_signal_near_full_risk():
    result = build_position_risk_multiplier({
        "atr_pct": 0.8,
        "meta_probability": 0.72,
        "trend_health_multiplier": 1.0,
        "strategy_quality_multiplier": 1.0,
        "regime_risk_multiplier": 1.0,
        "drawdown_pct": 0,
        "consecutive_losses": 0,
    })

    assert result["blocked"] is False
    assert result["risk_multiplier"] == 1.0


def test_adaptive_risk_reduces_and_caps_multiplier_stack():
    risk = calculate_adaptive_risk_pct(
        {"side": "long"},
        {"atr_percentile": 90, "account": {"consecutive_losses": 2}, "portfolio": {}},
        {"risk_multiplier": 1.0},
        {"size_multiplier": 1.0},
        {"size_multiplier": 1.2},
        {"risk_per_trade_pct": 0.8, "max_risk_per_trade_pct": 1.0},
    )

    assert 0 < risk <= 1.0
    assert risk < 0.8


def test_adaptive_risk_blocks_after_three_losses_daily_loss_and_exposure():
    cfg = {"risk_per_trade_pct": 0.5}

    assert calculate_adaptive_risk_pct(context={"account": {"consecutive_losses": 3}}, config=cfg) == 0.0
    assert calculate_adaptive_risk_pct(context={"account": {"daily_loss_r": -2.0}}, config=cfg) == 0.0
    assert calculate_adaptive_risk_pct(context={"portfolio": {"total_open_risk_pct": 2.0}}, config=cfg) == 0.0
    assert calculate_adaptive_risk_pct(context={"portfolio": {"same_direction_positions": 2}}, config=cfg) == 0.0


def test_aggressive_growth_disabled_keeps_base_plan_unchanged():
    base_plan = {"qty": 0.25, "risk_usdt": 1.0, "planned_notional": 25.0}

    result = build_aggressive_growth_overlay_plan(base_plan, {"aggressive_growth_enabled": False}, {})

    assert result["enabled"] is False
    assert result["accepted"] is False
    assert result["plan"] == base_plan


def test_aggressive_growth_enabled_caps_qty_by_risk_and_sleeve():
    base_plan = {
        "side": "long",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "risk_distance": 10.0,
        "leverage": 5,
    }

    result = build_aggressive_growth_overlay_plan(
        base_plan,
        {"aggressive_growth_enabled": True, "aggressive_growth_max_symbol_exposure_pct": 0.20},
        {
            "side": "long",
            "entry_price": 100.0,
            "stop_loss_price": 90.0,
            "account_equity": 1000.0,
            "growth_score": 70.0,
            "daily_pnl_usdt": 0.0,
            "weekly_pnl_usdt": 0.0,
            "open_positions": 0,
        },
    )

    assert result["accepted"] is True
    plan = result["plan"]
    assert plan["aggressive_growth_overlay"] is True
    assert plan["aggressive_growth_sleeve_cap_usdt"] == pytest.approx(200.0)
    assert plan["qty"] == pytest.approx(min(15.0 / 10.0, 200.0 / 100.0))
    assert plan["risk_usdt"] <= 15.0
    assert plan["planned_notional"] <= 200.0
    assert plan["partial_take_profit_ratio"] == pytest.approx(0.35)
    assert plan["second_take_profit_ratio"] == pytest.approx(0.30)
    assert plan["runner_pct"] == pytest.approx(0.35)


def test_aggressive_growth_daily_loss_limit_blocks_overlay():
    result = build_aggressive_growth_overlay_plan(
        {"side": "long", "entry_price": 100.0, "stop_loss": 90.0, "risk_distance": 10.0},
        {"aggressive_growth_enabled": True},
        {
            "side": "long",
            "entry_price": 100.0,
            "stop_loss_price": 90.0,
            "account_equity": 1000.0,
            "growth_score": 80.0,
            "daily_pnl_usdt": -40.0,
            "weekly_pnl_usdt": 0.0,
            "open_positions": 0,
        },
    )

    assert result["accepted"] is False
    assert "daily loss limit" in result["reason"]


def test_aggressive_growth_pyramiding_requires_profit_and_breakeven_sl():
    cfg = {"aggressive_growth_enabled": True}
    losing = build_aggressive_growth_pyramid_plan(
        {"side": "long", "entry_price": 100.0, "risk_distance": 10.0, "initial_qty": 1.0},
        cfg,
        {"current_price": 95.0, "stop_loss_price": 90.0, "account_equity": 1000.0},
    )
    assert losing["accepted"] is False
    assert "below trigger" in losing["reason"]

    blocked = build_aggressive_growth_pyramid_plan(
        {"side": "long", "entry_price": 100.0, "risk_distance": 10.0, "initial_qty": 1.0},
        cfg,
        {
            "current_price": 110.0,
            "stop_loss_price": 90.0,
            "account_equity": 1000.0,
            "sl_can_move_to_breakeven": False,
        },
    )
    assert blocked["accepted"] is False
    assert "breakeven" in blocked["reason"]

    allowed = build_aggressive_growth_pyramid_plan(
        {"side": "long", "entry_price": 100.0, "risk_distance": 10.0, "initial_qty": 1.0},
        cfg,
        {
            "current_price": 110.0,
            "stop_loss_price": 90.0,
            "account_equity": 1000.0,
            "sl_can_move_to_breakeven": True,
        },
    )
    assert allowed["accepted"] is True
    assert allowed["requires_sl_move"] is True
    assert allowed["add_qty"] <= 0.5
    assert allowed["worst_loss_usdt"] <= 15.0
