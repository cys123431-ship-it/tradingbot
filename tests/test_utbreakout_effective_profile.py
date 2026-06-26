import emas
from global_single_position_guard import OPPORTUNITY_OVERRIDES
from utbreakout_live_hardening_patch import PROFIT_MAX_OVERRIDES


def test_profit_opportunity_effective_profile_overrides_old_values():
    cfg = {
        "selection_mode": "manual",
        "auto_select_enabled": False,
        "live_auto_set_whitelist": [47],
        "partial_take_profit_r_multiple": 1.20,
        "partial_take_profit_ratio": 0.35,
        "second_take_profit_r_multiple": 2.50,
        "second_take_profit_ratio": 0.35,
        "take_profit_r_multiple": 2.0,
        "max_daily_trades": 10,
        "bias_continuation_min_volume_ratio": 0.75,
        "bias_continuation_15m_min_volume_ratio": 0.80,
        "selected_set_core_filter_hard_block_enabled": False,
        "atr_trailing_enabled": False,
        "runner_exit_enabled": False,
    }

    out = emas.apply_profit_opportunity_effective_overrides(cfg)

    assert out["effective_profile_version"] == "ev_adaptive_v2"
    assert out["selection_mode"] == "auto"
    assert out["auto_select_enabled"] is True
    assert out["active_set_id"] == 64
    assert out["profile"] == "set64"
    assert out["live_auto_set_whitelist"] == [64]
    assert out["partial_take_profit_r_multiple"] == 1.00
    assert out["partial_take_profit_ratio"] == 0.30
    assert out["second_take_profit_r_multiple"] == 2.00
    assert out["second_take_profit_ratio"] == 0.40
    assert out["take_profit_r_multiple"] == 2.00
    assert out["max_daily_trades"] == 7
    assert out["bias_continuation_min_volume_ratio"] == 0.40
    assert out["bias_continuation_15m_min_volume_ratio"] == 0.45
    assert out["selected_set_core_filter_hard_block_enabled"] is False
    assert out["atr_trailing_enabled"] is True
    assert out["runner_exit_enabled"] is True


def test_runtime_config_path_reapplies_effective_profile_after_persisted_values():
    engine = object.__new__(emas.SignalEngine)
    params = {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
        "UTBotFilteredBreakoutV1": {
            "selection_mode": "manual",
            "auto_select_enabled": False,
            "active_set_id": 47,
            "partial_take_profit_r_multiple": 1.20,
            "second_take_profit_r_multiple": 2.50,
            "take_profit_r_multiple": 2.0,
            "max_daily_trades": 10,
            "bias_continuation_min_volume_ratio": 0.75,
            "bias_continuation_15m_min_volume_ratio": 0.80,
        },
    }

    cfg = engine._get_utbot_filtered_breakout_config(params)

    assert cfg["effective_profile_version"] == emas.UTBREAKOUT_EFFECTIVE_PROFILE_VERSION
    assert cfg["selection_mode"] == "auto"
    assert cfg["auto_select_enabled"] is True
    assert cfg["active_set_id"] == 64
    assert cfg["profile"] == "set64"
    assert cfg["live_auto_set_whitelist"] == [64]
    assert cfg["partial_take_profit_r_multiple"] == 1.00
    assert cfg["partial_take_profit_ratio"] == 0.30
    assert cfg["second_take_profit_r_multiple"] == 2.00
    assert cfg["take_profit_r_multiple"] == 2.00
    assert cfg["max_daily_trades"] == 7
    assert cfg["bias_continuation_min_volume_ratio"] == 0.40
    assert cfg["bias_continuation_15m_min_volume_ratio"] == 0.45


def test_status_render_contract_replaces_stale_telegram_summary_values():
    stale = "\n".join([
        "🚦 UT Breakout 조건 스테이터스",
        "익절 계획: 2.0R",
        "Effective TP2: 2.00R",
        "Effective volume: base 0.75 / 15m 0.80",
        "일일 리스크: trades 0/10",
        "Bias continuation: volume ratio 0.25<0.75",
        "전략 적응 요약: exit partial 62%@1.15R, trail 1.65ATR from 1.15R",
        "선택 Set: Set47",
    ])

    rendered = emas.enforce_utbreakout_effective_status_contract(
        stale,
        {},
        daily_entries=0,
    )

    assert "Effective Profile: ev_adaptive_v2" in rendered
    assert "Strategy Router: EV Adaptive" in rendered
    assert "Effective TP2: 2.00R" in rendered
    assert "Effective volume: base 0.40 / 15m 0.45" in rendered
    assert "익절 계획: TP1 1.00R(30%) / TP2 2.00R(40%)" in rendered
    assert "일일 리스크: trades 0/7" in rendered
    assert "익절 계획: 2.0R" not in rendered
    assert "trades 0/10" not in rendered
    assert "volume ratio 0.25<0.75" not in rendered
    assert "volume ratio 0.25<0.45" in rendered
    assert "exit partial 62%@1.15R" not in rendered
    assert "trail 2.70-3.20ATR" in rendered
    assert "선택 Set: Set47" not in rendered
    assert "선택 Set: Set64 (effective AUTO fallback; legacy Set47 blocked)" in rendered


def test_prelaunch_runtime_patches_cannot_restore_the_retired_profile():
    effective = emas.apply_profit_opportunity_effective_overrides({})
    keys = (
        "effective_profile_version",
        "live_auto_set_whitelist",
        "active_set_id",
        "profile",
        "selected_set_core_filter_hard_block_enabled",
        "final_risk_multiplier_floor",
        "aggressive_growth_enabled",
        "aggressive_growth_pyramiding_enabled",
        "partial_take_profit_ratio",
        "second_take_profit_r_multiple",
        "runner_pct",
        "atr_trailing_activation_r",
        "atr_trailing_multiplier",
        "max_daily_trades",
    )

    for key in keys:
        assert OPPORTUNITY_OVERRIDES[key] == effective[key]
        assert PROFIT_MAX_OVERRIDES[key] == effective[key]
