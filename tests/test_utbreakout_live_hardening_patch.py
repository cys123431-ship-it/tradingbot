import utbreakout_live_hardening_patch as patch


def test_ev_router_keeps_legacy_set_hardening_disabled():
    cfg = {"selected_set_core_filter_hard_block_enabled": True}

    effective = patch.enforce_core_hardening_config(cfg)

    assert effective["selected_set_core_filter_hard_block_enabled"] is False


def test_profit_patch_matches_opportunity_profile_values():
    cfg = patch.tune_effective_config({})

    assert cfg["effective_profile_version"] == "ev_adaptive_v3_profit_engine"
    assert cfg["live_auto_set_whitelist"] == [64]
    assert cfg["active_set_id"] == 64
    assert cfg["partial_take_profit_r_multiple"] == 1.0
    assert cfg["partial_take_profit_ratio"] == 0.25
    assert cfg["second_take_profit_r_multiple"] == 2.4
    assert cfg["second_take_profit_ratio"] == 0.35
    assert cfg["dynamic_take_profit_enabled"] is False
    assert cfg["atr_trailing_multiplier"] == 3.0
    assert cfg["market_quality_min_risk_multiplier"] == 0.0
    assert cfg["max_daily_trades"] == 5
    assert cfg["profit_alpha_enabled"] is True
    assert cfg["profit_alpha_min_score"] == 68.0
    assert cfg["entry_quality_gate_min_profit_alpha_score"] == 68.0
    assert cfg["entry_edge_enabled"] is True
    assert cfg["entry_edge_min_score"] == 68.0
    assert cfg["entry_quality_gate_min_entry_edge_score"] == 68.0
    assert cfg["direction_engine_min_score"] == 62.0
    assert cfg["structure_stop_buffer_atr"] == 0.28
    assert cfg["take_profit_front_run_pct"] == 0.055
    assert cfg["soft_stop_confirm_bars"] == 2
    assert cfg["near_miss_tp_enabled"] is True
    assert cfg["market_regime_engine_enabled"] is True
    assert cfg["data_quality_engine_enabled"] is True
    assert cfg["execution_quality_engine_enabled"] is True
    assert cfg["protection_health_execution_gate_enabled"] is True
    assert cfg["overfit_governance_enabled"] is True


def test_mark_core_filter_failure_as_hard_block_compatible():
    items = [{
        "name": "상대 거래량",
        "state": False,
        "detail": "relVol 2.00 but candle direction failed",
        "code": "REJECTED_RELATIVE_VOLUME_CORE",
    }]

    hardened = patch.mark_core_set_filter_failures_hard(
        items,
        {"selected_set_core_filter_hard_block_enabled": True},
    )

    assert hardened[0]["core_set_hard_block"] is True
    assert hardened[0]["hard_block_original_name"] == "상대 거래량"
    assert hardened[0]["hard_block_original_code"] == "REJECTED_RELATIVE_VOLUME_CORE"
    assert hardened[0]["name"] == "유동성"
    assert "상대 거래량" in hardened[0]["detail"]


def test_non_core_filter_failure_is_left_unchanged():
    item = {
        "name": "보조 필터",
        "state": False,
        "detail": "weak auxiliary confirmation",
        "code": "SET_FILTER_SOFT_FAIL",
    }

    hardened = patch.mark_core_set_filter_failures_hard(
        [item],
        {"selected_set_core_filter_hard_block_enabled": True},
    )

    assert hardened[0] == item


def test_patch_signal_engine_wraps_config_and_filter_methods():
    class DummySignalEngine:
        def _get_utbot_filtered_breakout_config(self, strategy_params=None):
            return {"selected_set_core_filter_hard_block_enabled": False}

        def _evaluate_utbreakout_set_filter_items(self, side, set_info, cfg, values):
            return [{
                "name": "Donchian 돌파",
                "state": False,
                "detail": "no breakout",
                "code": "REJECTED_DONCHIAN_NO_BREAKOUT",
            }]

    assert patch.patch_signal_engine(DummySignalEngine) is True
    engine = DummySignalEngine()

    cfg = engine._get_utbot_filtered_breakout_config({})
    items = engine._evaluate_utbreakout_set_filter_items("long", {}, cfg, {})

    assert cfg["selected_set_core_filter_hard_block_enabled"] is False
    assert "core_set_hard_block" not in items[0]
    assert items[0]["name"] == "Donchian 돌파"
