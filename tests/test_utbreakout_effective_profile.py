import emas


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

    assert out["effective_profile_version"] == "profit_opportunity_v4_tp350_runner"
    assert out["selection_mode"] == "auto"
    assert out["auto_select_enabled"] is True
    assert out["active_set_id"] == 22
    assert out["profile"] == "set22"
    assert out["live_auto_set_whitelist"] == [5, 12, 22, 32, 51, 63]
    assert out["partial_take_profit_r_multiple"] == 1.00
    assert out["partial_take_profit_ratio"] == 0.20
    assert out["second_take_profit_r_multiple"] == 3.50
    assert out["second_take_profit_ratio"] == 0.40
    assert out["take_profit_r_multiple"] >= 3.50
    assert out["max_daily_trades"] == 7
    assert out["bias_continuation_min_volume_ratio"] == 0.40
    assert out["bias_continuation_15m_min_volume_ratio"] == 0.45
    assert out["selected_set_core_filter_hard_block_enabled"] is True
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
    assert cfg["active_set_id"] == 22
    assert cfg["profile"] == "set22"
    assert cfg["live_auto_set_whitelist"] == [5, 12, 22, 32, 51, 63]
    assert cfg["partial_take_profit_r_multiple"] == 1.00
    assert cfg["second_take_profit_r_multiple"] == 3.50
    assert cfg["take_profit_r_multiple"] == 3.50
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

    assert "Effective Profile: profit_opportunity_v4_tp350_runner" in rendered
    assert "Effective TP2: 3.50R" in rendered
    assert "Effective volume: base 0.40 / 15m 0.45" in rendered
    assert "익절 계획: TP1 1.00R(20%) / TP2 3.50R(40%)" in rendered
    assert "일일 리스크: trades 0/7" in rendered
    assert "익절 계획: 2.0R" not in rendered
    assert "Effective TP2: 2.00R" not in rendered
    assert "trades 0/10" not in rendered
    assert "volume ratio 0.25<0.75" not in rendered
    assert "volume ratio 0.25<0.45" in rendered
    assert "exit partial 62%@1.15R" not in rendered
    assert "trail 3.00-4.00ATR" in rendered
    assert "선택 Set: Set47" not in rendered
    assert "선택 Set: Set22 (effective AUTO fallback; legacy Set47 blocked)" in rendered
