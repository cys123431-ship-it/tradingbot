import asyncio

import bot_runtime.adaptive_research_patch as patch_module
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


def _fake_plan():
    return {
        "strategy": "adaptive_breakout_trend_v1",
        "qty": 6.0,
        "risk_usdt": 12.0,
        "planned_notional": 600.0,
        "planned_margin": 120.0,
        "adaptive_trend_target_qty": 10.0,
        "adaptive_trend_target_risk_usdt": 20.0,
        "adaptive_trend_target_notional": 1000.0,
        "position_cap_original_notional": 600.0,
        "adaptive_breakout_trend_score": 80.0,
        "adaptive_breakout_trend_risk_multiplier": 0.9,
        "adaptive_breakout_trend_target_risk_percent": 3.0,
        "adaptive_breakout_trend_metrics": {"score": 80.0},
        "stop_loss": 98.0,
        "risk_distance": 2.0,
        "partial_take_profit_r_multiple": 2.0,
        "adaptive_trend_pyramid_trigger_r": (0.5, 1.0, 1.5),
    }


def test_runtime_wrapper_scales_accepted_plan_without_touching_protection(monkeypatch):
    async def original(self, symbol, df, strategy_params, *, force_reprocess=False):
        plan = _fake_plan()
        return "long", "ACCEPTED_ENTRY", {
            "allowed": True,
            "score": 80.0,
            "risk_multiplier": 0.9,
            "metrics": {"score": 80.0},
            "small_account_aggressive_candidate": True,
            "entry_plan": plan,
        }

    monkeypatch.setattr(
        SignalAlphaMixin,
        "_calculate_adaptive_breakout_trend_signal",
        original,
    )
    monkeypatch.setattr(
        patch_module,
        "evaluate_adaptive_research_overlay",
        lambda *args, **kwargs: {
            "allowed": True,
            "code": "ADAPTIVE_RESEARCH_OVERLAY_ALLOWED",
            "reason": "test",
            "risk_multiplier": 0.5,
            "adjusted_score": 74.0,
        },
    )
    patch_module.install_adaptive_research_overlay()

    engine = SignalAlphaMixin()
    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._fetch_utbreakout_futures_context = lambda symbol: asyncio.sleep(
        0, result={"funding_rate": 0.0008, "basis_pct": 0.2}
    )
    engine._get_utbot_filtered_breakout_config = lambda params: {}
    engine._adaptive_breakout_trend_runtime_config = lambda cfg: {}
    stored = {}
    engine._set_utbot_filtered_breakout_entry_plan = (
        lambda symbol, plan: stored.update({"symbol": symbol, "plan": plan})
    )

    side, _, status = asyncio.run(
        engine._calculate_adaptive_breakout_trend_signal("TEST", None, {})
    )
    plan = status["entry_plan"]
    assert side == "long"
    assert plan["qty"] == 3.0
    assert plan["adaptive_trend_target_qty"] == 5.0
    assert plan["stop_loss"] == 98.0
    assert plan["risk_distance"] == 2.0
    assert plan["partial_take_profit_r_multiple"] == 2.0
    assert plan["adaptive_trend_pyramid_trigger_r"] == (0.5, 1.0, 1.5)
    assert status["risk_multiplier"] == 0.45
    assert stored["plan"] == plan


def test_runtime_wrapper_leaves_non_small_account_unchanged(monkeypatch):
    original_plan = _fake_plan()

    async def original(self, symbol, df, strategy_params, *, force_reprocess=False):
        return "long", "ACCEPTED_ENTRY", {
            "allowed": True,
            "score": 80.0,
            "risk_multiplier": 0.9,
            "metrics": {"score": 80.0},
            "small_account_aggressive_candidate": False,
            "entry_plan": dict(original_plan),
        }

    monkeypatch.setattr(
        SignalAlphaMixin,
        "_calculate_adaptive_breakout_trend_signal",
        original,
    )
    calls = []
    monkeypatch.setattr(
        patch_module,
        "evaluate_adaptive_research_overlay",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    patch_module.install_adaptive_research_overlay()

    engine = SignalAlphaMixin()
    side, reason, status = asyncio.run(
        engine._calculate_adaptive_breakout_trend_signal("TEST", None, {})
    )

    assert side == "long"
    assert reason == "ACCEPTED_ENTRY"
    assert status["entry_plan"] == original_plan
    assert calls == []
    assert "adaptive_research_overlay" not in status


def test_runtime_wrapper_clears_rejected_plan(monkeypatch):
    async def original(self, symbol, df, strategy_params, *, force_reprocess=False):
        return "long", "ACCEPTED_ENTRY", {
            "allowed": True,
            "score": 80.0,
            "metrics": {"score": 80.0},
            "small_account_aggressive_candidate": True,
            "entry_plan": _fake_plan(),
        }

    monkeypatch.setattr(
        SignalAlphaMixin,
        "_calculate_adaptive_breakout_trend_signal",
        original,
    )
    monkeypatch.setattr(
        patch_module,
        "evaluate_adaptive_research_overlay",
        lambda *args, **kwargs: {
            "allowed": False,
            "code": "REJECTED_RESEARCH_OVERLAY_TEST",
            "reason": "test reject",
            "risk_multiplier": 0.0,
            "adjusted_score": 50.0,
        },
    )
    patch_module.install_adaptive_research_overlay()

    engine = SignalAlphaMixin()
    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._fetch_utbreakout_futures_context = lambda symbol: asyncio.sleep(
        0, result={}
    )
    engine._get_utbot_filtered_breakout_config = lambda params: {}
    engine._adaptive_breakout_trend_runtime_config = lambda cfg: {}
    cleared = []
    engine._clear_utbot_filtered_breakout_entry_plan = cleared.append

    side, reason, status = asyncio.run(
        engine._calculate_adaptive_breakout_trend_signal("TEST", None, {})
    )
    assert side is None
    assert cleared == ["TEST"]
    assert status["allowed"] is False
    assert "entry_plan" not in status
    assert status["reject_code"] == "REJECTED_RESEARCH_OVERLAY_TEST"
    assert "research overlay waiting" in reason.lower()
