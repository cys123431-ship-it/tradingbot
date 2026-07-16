import pytest

import emas
from utbreakout.sizing import (
    apply_aggressive_volatility_targeting,
    build_aggressive_growth_overlay_plan,
    build_aggressive_growth_pyramid_plan,
    build_position_risk_multiplier,
    calculate_adaptive_risk_pct,
    calculate_aggressive_sleeve_notional_cap,
    calculate_cppi_sleeve_pct,
    calculate_fractional_kelly_multiplier,
    choose_aggressive_exit_split,
    evaluate_derivatives_growth_score,
    is_aggressive_symbol_trend_bullish,
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


def test_position_sizing_block_wins_over_positive_minimum_multiplier():
    result = build_position_risk_multiplier(
        {"daily_loss_limit_hit": True},
        {"min_risk_multiplier": 0.25},
    )

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


def test_position_sizing_reduces_weak_edge_without_blocking():
    result = build_position_risk_multiplier({
        "atr_pct": 0.9,
        "meta_probability": 0.57,
        "entry_edge_score": 66.0,
        "entry_edge_probability": 0.565,
    })

    assert result["blocked"] is False
    assert 0 < result["risk_multiplier"] < 1.0
    assert result["components"]["edge_score"] < 1.0
    assert result["components"]["edge_probability"] < 1.0


def test_position_sizing_uses_fractional_kelly_as_conservative_cap():
    trades = [{"r_multiple": -1.0}] * 14 + [{"r_multiple": 0.6}] * 8

    result = build_position_risk_multiplier({
        "atr_pct": 0.8,
        "meta_probability": 0.70,
        "recent_trades": trades,
    })

    assert result["blocked"] is False
    assert result["components"]["kelly"] < 1.0
    assert result["risk_multiplier"] <= result["components"]["kelly"]
    assert result["kelly_reason"] in {"negative_kelly", "kelly_applied"}


def test_position_sizing_blocks_negative_meta_expectancy_with_samples():
    result = build_position_risk_multiplier({
        "atr_pct": 0.8,
        "meta_probability": 0.70,
        "recent_avg_pnl_r": -0.50,
        "meta_sample_count": 12,
    })

    assert result["blocked"] is True
    assert result["risk_multiplier"] == 0.0
    assert any("negative expectancy" in reason for reason in result["reasons"])


def test_position_sizing_ignores_negative_meta_expectancy_without_samples():
    result = build_position_risk_multiplier({
        "atr_pct": 0.8,
        "meta_probability": 0.70,
        "recent_avg_pnl_r": -0.50,
        "meta_sample_count": 0,
    })

    assert result["blocked"] is False
    assert result["components"]["recent_performance"] == 1.0
    assert not any("performance" in reason for reason in result["reasons"])


def test_position_sizing_rave_like_edge_keeps_independent_risk_brakes():
    result = build_position_risk_multiplier({
        "atr_pct": 1.9068,
        "meta_probability": 0.65,
        "entry_edge_probability": 0.5837,
        "entry_edge_score": 88.7,
        "recent_avg_pnl_r": -0.90,
        "meta_sample_count": 0,
        "recent_closed_pnls": [-0.34, -0.38, -0.92],
    })

    assert result["blocked"] is False
    assert result["components"]["meta"] == 1.0
    assert result["components"]["recent_performance"] == 1.0
    assert result["components"]["volatility"] < 1.0
    assert result["components"]["consecutive_loss"] == pytest.approx(0.6)
    assert result["risk_multiplier"] == pytest.approx(0.3146, abs=0.001)


def test_position_sizing_reduces_or_blocks_portfolio_heat():
    reduced = build_position_risk_multiplier({
        "atr_pct": 0.8,
        "meta_probability": 0.70,
        "total_open_risk_pct": 1.5,
    })
    blocked = build_position_risk_multiplier({
        "atr_pct": 0.8,
        "meta_probability": 0.70,
        "total_open_risk_pct": 2.2,
    })

    assert reduced["blocked"] is False
    assert 0 < reduced["risk_multiplier"] < 1.0
    assert blocked["blocked"] is True
    assert blocked["risk_multiplier"] == 0.0


def test_utbreakout_position_sizing_portfolio_context_counts_positions():
    engine = object.__new__(emas.SignalEngine)

    context = engine._build_utbreakout_position_sizing_portfolio_context(
        symbol="SOL/USDT:USDT",
        side="long",
        cfg={"risk_per_trade_percent": 0.4},
        positions=[
            {"symbol": "SOL/USDT:USDT", "side": "long", "contracts": 1.0},
            {"symbol": "ETH/USDT:USDT", "side": "short", "contracts": 2.0},
            {"symbol": "XRP/USDT:USDT", "side": "long", "contracts": 0.0},
        ],
    )

    assert context["open_positions"] == 2
    assert context["same_direction_positions"] == 1
    assert context["total_open_risk_pct"] == pytest.approx(0.8)


def test_recent_strategy_pnls_exclude_retired_strategy_losses():
    engine = object.__new__(emas.SignalEngine)

    class Store:
        def load_trade_results(self):
            return [
                {
                    "strategy": "ut_breakout",
                    "exit_time": "2026-07-16T02:45:00+00:00",
                    "net_pnl_usdt": -0.34,
                },
                {
                    "strategy": "multi_timeframe_trend_v1",
                    "exit_time": "2026-07-16T03:00:00+00:00",
                    "net_pnl_usdt": -0.92,
                },
                {
                    "primary_strategy": "ut_breakout",
                    "exit_time": "2026-07-16T03:45:00+00:00",
                    "net_pnl_usdt": -0.38,
                },
            ]

    engine.trading_state_store = Store()
    engine.db = None

    pnls = engine._get_recent_strategy_closed_trade_pnls(
        {"ut_breakout", "utbot_adaptive_timeframe_v1"},
        5,
    )

    assert pnls == pytest.approx([-0.38, -0.34])
    sizing = build_position_risk_multiplier({
        "atr_pct": 1.0,
        "meta_probability": 0.65,
        "recent_closed_pnls": pnls,
    })
    assert sizing["components"]["consecutive_loss"] == pytest.approx(0.8)


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
            "atr_pct": 0.012,
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


def test_aggressive_growth_sleeve_mode_notional_and_margin():
    base_plan = {"side": "long", "entry_price": 100.0, "stop_loss": 99.0, "risk_distance": 1.0, "leverage": 5}
    context = {
        "side": "long",
        "entry_price": 100.0,
        "stop_loss_price": 99.0,
        "account_equity": 1000.0,
        "growth_score": 70.0,
        "atr_pct": 0.012,
        "open_positions": 0,
        "leverage": 5,
    }
    cfg = {"aggressive_growth_enabled": True, "aggressive_growth_max_symbol_exposure_pct": 1.0}

    notional = build_aggressive_growth_overlay_plan(base_plan, cfg, context)
    margin = build_aggressive_growth_overlay_plan(
        base_plan,
        {**cfg, "aggressive_growth_sleeve_mode": "margin", "aggressive_growth_max_leverage_for_margin_sleeve": 3.0},
        context,
    )

    assert notional["accepted"] is True
    assert margin["accepted"] is True
    assert notional["plan"]["aggressive_growth_sleeve_cap_usdt"] == pytest.approx(200.0)
    assert margin["plan"]["aggressive_growth_sleeve_cap_usdt"] == pytest.approx(600.0)
    assert notional["plan"]["qty"] == pytest.approx(2.0)
    assert margin["plan"]["qty"] == pytest.approx(6.0)


def test_aggressive_symbol_trend_strict_parser():
    assert is_aggressive_symbol_trend_bullish({"state": True}) is True
    assert is_aggressive_symbol_trend_bullish({"state": "strong"}) is True
    assert is_aggressive_symbol_trend_bullish({"state": "uptrend"}) is True
    assert is_aggressive_symbol_trend_bullish({"state": None}) is False
    assert is_aggressive_symbol_trend_bullish({"state": False}) is False
    assert is_aggressive_symbol_trend_bullish({}) is False


def test_aggressive_derivatives_growth_score_rewards_confirmation_and_penalizes_crowding():
    positive, positive_reasons = evaluate_derivatives_growth_score({
        "funding_rate": 0.0001,
        "funding_percentile_7d": 45,
        "open_interest_change_1h": 0.8,
        "price_change_1h": 1.2,
        "long_short_ratio": 1.2,
        "taker_buy_sell_ratio": 1.08,
        "liquidation_imbalance": 0.3,
    })
    negative, negative_reasons = evaluate_derivatives_growth_score({
        "funding_rate": 0.0012,
        "funding_percentile_30d": 95,
        "open_interest_change_1h": 1.1,
        "price_change_1h": -0.2,
        "long_short_ratio": 2.8,
        "taker_buy_sell_ratio": 0.9,
    })

    assert positive > 0
    assert "price_up_oi_up" in positive_reasons
    assert negative < 0
    assert "funding_overheated" in negative_reasons
    assert "long_crowded" in negative_reasons


def test_aggressive_dynamic_exit_split_sums_to_one():
    assert choose_aggressive_exit_split(90) == {
        "tp1_pct": 0.2,
        "tp2_pct": 0.2,
        "runner_pct": 0.6,
        "mode": "super_strong_trend",
    }
    assert choose_aggressive_exit_split(80)["runner_pct"] == pytest.approx(0.5)
    assert choose_aggressive_exit_split(65)["runner_pct"] == pytest.approx(0.35)
    weak = choose_aggressive_exit_split(40)
    assert weak["tp1_pct"] + weak["tp2_pct"] + weak["runner_pct"] == pytest.approx(1.0)


def test_aggressive_volatility_targeting_reduces_or_blocks_risk():
    cfg = {"aggressive_growth_vol_target_enabled": True}

    risk, reason = apply_aggressive_volatility_targeting(0.01, None, cfg)
    assert risk == pytest.approx(0.008)
    assert reason == "atr_missing"
    risk, reason = apply_aggressive_volatility_targeting(0.01, 0.003, cfg)
    assert risk == pytest.approx(0.008)
    assert reason == "atr_too_low_reduce"
    risk, reason = apply_aggressive_volatility_targeting(0.01, 0.030, cfg)
    assert risk == pytest.approx(0.005)
    assert reason == "atr_high_reduce"
    risk, reason = apply_aggressive_volatility_targeting(0.01, 0.040, cfg)
    assert risk == pytest.approx(0.0)
    assert reason == "atr_extreme_block"


def test_aggressive_fractional_kelly_multiplier_defaults_and_bounds():
    assert calculate_fractional_kelly_multiplier([], {}) == (1.0, "kelly_disabled")
    assert calculate_fractional_kelly_multiplier([], {"aggressive_growth_kelly_enabled": True}) == (0.75, "not_enough_trades")

    negative_trades = [{"r_multiple": -1.0}] * 25 + [{"r_multiple": 0.5}] * 5
    negative, reason = calculate_fractional_kelly_multiplier(negative_trades, {"aggressive_growth_kelly_enabled": True})
    assert negative == pytest.approx(0.25)
    assert reason == "negative_kelly"

    positive_trades = [{"r_multiple": 1.5}] * 35 + [{"r_multiple": -1.0}] * 15
    positive, reason = calculate_fractional_kelly_multiplier(
        positive_trades,
        {"aggressive_growth_kelly_enabled": True, "aggressive_growth_kelly_min_risk_multiplier": 0.0},
    )
    assert 0 < positive <= 1.25
    assert reason == "kelly_applied"


def test_aggressive_cppi_sleeve_and_margin_cap():
    cfg = {
        "aggressive_growth_cppi_enabled": True,
        "aggressive_growth_cppi_floor_pct": 0.90,
        "aggressive_growth_cppi_multiplier": 2.0,
        "aggressive_growth_cppi_min_sleeve_pct": 0.05,
        "aggressive_growth_cppi_max_sleeve_pct": 0.20,
    }

    assert calculate_cppi_sleeve_pct(1000, 1200, cfg) == pytest.approx(0.05)
    assert calculate_cppi_sleeve_pct(1300, 1300, cfg) == pytest.approx(0.20)

    cap, sleeve = calculate_aggressive_sleeve_notional_cap(
        1300,
        leverage=5,
        cfg={**cfg, "aggressive_growth_sleeve_mode": "margin", "aggressive_growth_max_leverage_for_margin_sleeve": 3.0},
        high_watermark=1300,
    )
    assert sleeve == pytest.approx(0.20)
    assert cap == pytest.approx(1300 * 0.20 * 3.0)


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


def test_aggressive_growth_pyramiding_blocks_bad_growth_context_and_loss_limits():
    cfg = {"aggressive_growth_enabled": True}
    base_position = {"side": "long", "entry_price": 100.0, "risk_distance": 10.0, "initial_qty": 1.0}

    bad_context = build_aggressive_growth_pyramid_plan(
        base_position,
        cfg,
        {
            "current_price": 112.0,
            "stop_loss_price": 100.0,
            "account_equity": 1000.0,
            "growth_context_valid": False,
        },
    )
    daily_block = build_aggressive_growth_pyramid_plan(
        base_position,
        cfg,
        {
            "current_price": 112.0,
            "stop_loss_price": 100.0,
            "account_equity": 1000.0,
            "daily_pnl_usdt": -40.0,
        },
    )

    assert bad_context["accepted"] is False
    assert "growth context" in bad_context["reason"]
    assert daily_block["accepted"] is False
    assert "daily loss limit" in daily_block["reason"]
