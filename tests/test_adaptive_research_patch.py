from bot_runtime.adaptive_research_patch import scale_adaptive_trend_plan
from bot_runtime.signal_alpha import SignalAlphaMixin


def test_scale_plan_only_reduces_size_and_risk_fields():
    plan = {
        "strategy": "adaptive_breakout_trend_v1",
        "qty": 6.5,
        "risk_usdt": 20.0,
        "planned_notional": 650.0,
        "planned_margin": 130.0,
        "expected_profit_usdt": 50.0,
        "adaptive_trend_target_qty": 10.0,
        "adaptive_trend_target_risk_usdt": 30.0,
        "adaptive_trend_target_notional": 1000.0,
        "position_cap_original_notional": 650.0,
        "adaptive_breakout_trend_risk_multiplier": 0.8,
        "adaptive_breakout_trend_target_risk_percent": 3.0,
        "stop_loss": 98.0,
        "hard_stop_loss": 98.0,
        "risk_distance": 2.0,
        "partial_take_profit_r_multiple": 2.0,
        "runner_pct": 0.85,
        "adaptive_trend_pyramid_trigger_r": (0.5, 1.0, 1.5),
        "adaptive_trend_pyramid_target_fractions": (0.8, 0.9, 1.0),
    }
    adjusted = scale_adaptive_trend_plan(plan, 0.5, {"allowed": True})

    assert adjusted["qty"] == 3.25
    assert adjusted["adaptive_trend_target_qty"] == 5.0
    assert adjusted["risk_usdt"] == 10.0
    assert adjusted["adaptive_breakout_trend_risk_multiplier"] == 0.4
    assert adjusted["adaptive_breakout_trend_target_risk_percent"] == 1.5

    for key in (
        "stop_loss",
        "hard_stop_loss",
        "risk_distance",
        "partial_take_profit_r_multiple",
        "runner_pct",
        "adaptive_trend_pyramid_trigger_r",
        "adaptive_trend_pyramid_target_fractions",
    ):
        assert adjusted[key] == plan[key]


def test_scale_plan_can_never_upsize():
    plan = {
        "qty": 2.0,
        "adaptive_trend_target_qty": 4.0,
        "planned_notional": 200.0,
    }
    adjusted = scale_adaptive_trend_plan(plan, 1.8)
    assert adjusted["qty"] == 2.0
    assert adjusted["adaptive_trend_target_qty"] == 4.0
    assert adjusted["planned_notional"] == 200.0


def test_scale_plan_zero_is_fail_closed_size():
    plan = {"qty": 2.0, "risk_usdt": 5.0, "stop_loss": 98.0}
    adjusted = scale_adaptive_trend_plan(plan, 0.0)
    assert adjusted["qty"] == 0.0
    assert adjusted["risk_usdt"] == 0.0
    assert adjusted["stop_loss"] == 98.0


def test_runtime_overlay_installer_is_active_on_research_branch():
    method = SignalAlphaMixin._calculate_adaptive_breakout_trend_signal
    assert getattr(method, "_adaptive_research_overlay_installed", False) is True
    assert callable(getattr(method, "__runtime_original__", None))
