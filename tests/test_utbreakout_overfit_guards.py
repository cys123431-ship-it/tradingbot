import pytest

from emas import (
    SignalEngine,
    _apply_utbreakout_risk_multiplier_floor,
    apply_stable_utbreak_final_overrides,
    build_default_utbot_filtered_breakout_config,
    build_utbreakout_effective_config_diff_text,
    get_utbreakout_set_definition,
    validate_utbreakout_runtime_set_id,
)


def _engine():
    return object.__new__(SignalEngine)


def test_profit_opportunity_final_overrides_are_authoritative():
    cfg = apply_stable_utbreak_final_overrides({
        "partial_take_profit_r_multiple": 1.2,
        "second_take_profit_r_multiple": 2.5,
        "bias_continuation_min_volume_ratio": 0.5,
        "selected_set_core_filter_hard_block_enabled": False,
    })

    assert cfg["live_auto_set_whitelist"] == [5, 12, 22, 32, 51, 63]
    assert cfg["selected_set_core_filter_hard_block_enabled"] is True
    assert cfg["partial_take_profit_r_multiple"] == 1.0
    assert cfg["partial_take_profit_ratio"] == 0.20
    assert cfg["second_take_profit_r_multiple"] == 3.5
    assert cfg["second_take_profit_ratio"] == 0.40
    assert cfg["bias_continuation_min_volume_ratio"] == 0.40
    assert cfg["bias_continuation_15m_min_volume_ratio"] == 0.45
    assert cfg["max_daily_trades"] == 7
    assert cfg["runner_chandelier_enabled"] is True


def test_accepted_soft_reductions_keep_meaningful_final_risk_floor():
    cfg = {"final_risk_multiplier_floor": 0.20}

    assert _apply_utbreakout_risk_multiplier_floor(0.016, cfg) == 0.20
    assert _apply_utbreakout_risk_multiplier_floor(0.35, cfg) == 0.35
    assert _apply_utbreakout_risk_multiplier_floor(0.0, cfg) == 0.0


def test_live_auto_whitelist_blocks_non_whitelisted_set():
    cfg = build_default_utbot_filtered_breakout_config()
    cfg.update({
        "live_trading": True,
        "selection_mode": "auto",
        "auto_select_enabled": True,
        "live_safety_guard_enabled": True,
        "live_auto_set_whitelist_enabled": True,
        "live_auto_set_whitelist": [22, 32],
        "safe_live_default_set_id": 22,
    })

    selected, reason = validate_utbreakout_runtime_set_id(cfg, 51, is_live=True)

    assert selected == 22
    assert "whitelist blocked" in reason


def test_live_auto_whitelist_allows_whitelisted_set():
    cfg = build_default_utbot_filtered_breakout_config()
    cfg.update({
        "live_trading": True,
        "selection_mode": "auto",
        "auto_select_enabled": True,
        "live_safety_guard_enabled": True,
        "live_auto_set_whitelist_enabled": True,
        "live_auto_set_whitelist": [22, 32],
        "safe_live_default_set_id": 22,
    })

    selected, reason = validate_utbreakout_runtime_set_id(cfg, 32, is_live=True)

    assert selected == 32
    assert reason is None


def test_set32_relative_volume_requires_direction_candle_ema_and_flow():
    engine = _engine()
    cfg = build_default_utbot_filtered_breakout_config()
    cfg.update({
        "set32_min_relative_volume": 1.50,
        "set32_require_direction_candle": True,
        "set32_require_ema50_side": True,
        "set32_require_orderflow_confirmation": True,
        "set32_orderflow_min_samples": 3,
        "set32_min_taker_ratio_long": 1.00,
        "set32_max_spread_pct": 0.05,
    })
    set32 = get_utbreakout_set_definition(32)

    bad_items = engine._evaluate_utbreakout_set_filter_items(
        "long",
        set32,
        cfg,
        {
            "volume_ratio": 2.0,
            "open": 610.0,
            "entry_price": 609.0,
            "ema50": 608.0,
            "taker_buy_sell_ratio": 1.02,
            "rolling_ofi_samples": 3,
            "futures_spread_pct": 0.01,
        },
    )

    assert bad_items
    assert bad_items[0]["name"] == "상대 거래량"
    assert bad_items[0]["state"] is False
    assert bad_items[0]["code"] == "REJECTED_RELATIVE_VOLUME_CORE"

    good_items = engine._evaluate_utbreakout_set_filter_items(
        "long",
        set32,
        cfg,
        {
            "volume_ratio": 2.0,
            "open": 608.0,
            "entry_price": 609.0,
            "ema50": 607.0,
            "taker_buy_sell_ratio": 1.02,
            "rolling_ofi_samples": 3,
            "futures_spread_pct": 0.01,
        },
    )

    assert good_items
    assert good_items[0]["state"] is True


def test_set32_relative_volume_rejects_low_orderflow_samples():
    engine = _engine()
    cfg = build_default_utbot_filtered_breakout_config()
    cfg.update({
        "set32_min_relative_volume": 1.50,
        "set32_require_direction_candle": True,
        "set32_require_ema50_side": True,
        "set32_require_orderflow_confirmation": True,
        "set32_orderflow_min_samples": 3,
        "set32_min_taker_ratio_long": 1.00,
        "set32_max_spread_pct": 0.05,
    })
    set32 = get_utbreakout_set_definition(32)

    items = engine._evaluate_utbreakout_set_filter_items(
        "long",
        set32,
        cfg,
        {
            "volume_ratio": 2.0,
            "open": 608.0,
            "entry_price": 609.0,
            "ema50": 607.0,
            "taker_buy_sell_ratio": 1.02,
            "rolling_ofi_samples": 2,
            "futures_spread_pct": 0.01,
        },
    )

    assert items[0]["state"] is False
    assert "samples 2/3" in items[0]["detail"]


def test_auto_weak_margin_sets_live_block_flag():
    engine = _engine()
    cfg = build_default_utbot_filtered_breakout_config()
    cfg.update({
        "live_trading": True,
        "mode": "live",
        "selection_mode": "auto",
        "auto_select_enabled": True,
        "live_auto_set_whitelist_enabled": True,
        "live_auto_set_whitelist": [22, 32, 51, 63],
        "auto_min_score_margin_live": 100.0,
        "auto_min_adjusted_score_live": 0.0,
        "auto_block_on_weak_margin_live": True,
        "auto_multiple_testing_penalty_enabled": False,
    })
    analysis = {
        "scores": {
            "ready_timeframes": 3,
            "trend_score": 60.0,
            "chop_score": 30.0,
            "volatility_score": 80.0,
            "breakout_score": 100.0,
            "momentum_score": 60.0,
            "flow_score": 90.0,
            "alignment_score": 60.0,
            "relative_volume_score": 95.0,
            "prediction_orderflow_score": 80.0,
            "prediction_depth_score": 50.0,
            "squeeze_release_score": 50.0,
            "range_expansion_score": 70.0,
        }
    }

    selected_id, reason = engine._select_utbreakout_auto_set(analysis, cfg)

    assert selected_id in {22, 32, 51, 63}
    assert analysis["scores"]["auto_blocked_by_margin"] is True
    assert "live AUTO weak edge" in analysis["scores"]["auto_block_reason"]


def test_live_long_market_quality_multi_adverse_hard_blocks():
    engine = _engine()
    cfg = build_default_utbot_filtered_breakout_config()
    cfg.update({
        "live_trading": True,
        "market_quality_long_hard_block_on_multi_adverse_enabled": True,
        "market_quality_long_multi_adverse_min_reasons": 3,
        "market_quality_long_multi_adverse_max_multiplier": 0.35,
    })

    result = engine._evaluate_utbreakout_market_quality(
        "long",
        cfg,
        {
            "atr_pct": 11.0,
            "funding_rate": 0.002,
            "futures_spread_pct": 0.02,
            "market_regime_context": {
                "items": {
                    "BTC/USDT": {
                        "direction": "short",
                        "return_lookback_pct": -2.0,
                    },
                },
            },
        },
    )

    assert result["state"] is False
    assert result["hard_block"] is True
    assert "LONG market multi-adverse" in result["summary"]


def test_config_diff_text_reports_effective_overrides():
    raw = {
        "partial_take_profit_r_multiple": 1.5,
        "second_take_profit_r_multiple": 2.0,
    }
    effective = {
        "runtime_profile": "stable_15m_direction_filter_v1",
        "partial_take_profit_r_multiple": 1.2,
        "second_take_profit_r_multiple": 2.5,
        "live_auto_set_whitelist": [22, 32, 51, 63],
    }

    text = build_utbreakout_effective_config_diff_text(raw, effective)

    assert "UT Breakout Config Diff" in text
    assert "partial_take_profit_r_multiple" in text
    assert "1.5 -> 1.2" in text
    assert "live_auto_set_whitelist" in text
