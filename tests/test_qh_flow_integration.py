import asyncio

import pytest


def _emas_module():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI")


def test_qh_and_triple_are_selectable_live_strategies():
    emas = _emas_module()
    assert emas.QH_FLOW_STRATEGY in emas.UTBREAKOUT_STRATEGIES
    assert emas.TRIPLE_ALPHA_STRATEGY in emas.UTBREAKOUT_STRATEGIES
    assert emas.STRATEGY_DISPLAY_NAMES[emas.QH_FLOW_STRATEGY] == "QH_FLOW"
    assert emas.STRATEGY_DISPLAY_NAMES[emas.TRIPLE_ALPHA_STRATEGY] == "TRIPLE_ALPHA"


def test_triple_branch_params_keep_three_engines_independent():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    params = {
        "active_strategy": emas.TRIPLE_ALPHA_STRATEGY,
        "UTBotFilteredBreakoutV1": {
            "qh_flow_confirmation_enabled": True,
            "qh_flow": {"qh_confirmation_enabled": True},
        },
    }

    ut = engine._triple_alpha_strategy_params(params, emas.ENTRY_STRATEGY_UT_BREAKOUT)
    rsp = engine._triple_alpha_strategy_params(
        params,
        emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    )
    qh = engine._triple_alpha_strategy_params(params, emas.QH_FLOW_STRATEGY)

    assert ut["UTBotFilteredBreakoutV1"]["qh_flow_confirmation_enabled"] is False
    assert rsp["UTBotFilteredBreakoutV1"]["qh_flow_confirmation_enabled"] is False
    assert rsp["active_strategy"] == emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
    assert qh["active_strategy"] == emas.QH_FLOW_STRATEGY
    assert qh["UTBotFilteredBreakoutV1"]["qh_flow"]["qh_flow_live_enabled"] is True


def test_l2_gate_fails_open_only_when_runtime_fetcher_does_not_exist():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    engine.ctrl = object()
    engine.l2_gate_cache = {}
    engine.is_upbit_mode = lambda: False
    engine._canonical_futures_symbol = lambda symbol: symbol

    result = asyncio.run(engine._evaluate_shared_l2_gate("BTC/USDT:USDT", {}))

    assert result["state"] == "unavailable"
    assert result["allowed"] is True
    assert result["risk_multiplier"] == 1.0


def test_qh_confirmation_can_be_disabled_for_independent_triple_branches():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    result = asyncio.run(
        engine._qh_flow_confirmation(
            "BTC/USDT:USDT",
            "long",
            {"qh_flow": {"qh_confirmation_enabled": False}},
        )
    )
    assert result["state"] == "disabled"
    assert result["allowed"] is True
    assert result["risk_multiplier"] == 1.0


def test_triple_plan_scaling_preserves_prices_and_scales_risk_fields():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    plan = {
        "qty": 2.0,
        "risk_usdt": 1.0,
        "planned_notional": 100.0,
        "planned_margin": 20.0,
        "entry_price": 50.0,
        "stop_loss": 48.0,
        "take_profit": 55.0,
    }
    scaled = engine._dual_alpha_scale_plan(plan, 0.55)
    assert scaled["qty"] == pytest.approx(1.10)
    assert scaled["risk_usdt"] == pytest.approx(0.55)
    assert scaled["planned_notional"] == pytest.approx(55.0)
    assert scaled["entry_price"] == 50.0
    assert scaled["stop_loss"] == 48.0
    assert scaled["take_profit"] == 55.0


def test_qh_and_triple_callback_actions_are_registered():
    emas = _emas_module()
    assert {
        "qh",
        "qhflow",
        "qh_status",
        "triple",
        "triplet",
        "triple_status",
    } <= emas.UTBREAKOUT_CALLBACK_ACTIONS
