import outbreakout_live_hardening_patch as patch


def test_enforce_core_hardening_config_forces_flag_on():
    cfg = {"selected_set_core_filter_hard_block_enabled": False}

    effective = patch.enforce_core_hardening_config(cfg)

    assert effective["selected_set_core_filter_hard_block_enabled"] is True


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

    assert cfg["selected_set_core_filter_hard_block_enabled"] is True
    assert items[0]["core_set_hard_block"] is True
    assert items[0]["name"] == "유동성"
