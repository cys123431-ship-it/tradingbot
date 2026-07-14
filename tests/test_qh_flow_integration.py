import asyncio

import pytest


def _emas_module():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI")


def test_qh_and_triple_are_selectable_live_strategies():
    emas = _emas_module()
    assert emas.QH_FLOW_STRATEGY in emas.UTBREAKOUT_STRATEGIES
    assert emas.TRIPLE_ALPHA_STRATEGY in emas.UTBREAKOUT_STRATEGIES
    assert emas.QUAD_ALPHA_STRATEGY in emas.UTBREAKOUT_STRATEGIES
    assert emas.STRATEGY_DISPLAY_NAMES[emas.QH_FLOW_STRATEGY] == "QH_FLOW"
    assert emas.STRATEGY_DISPLAY_NAMES[emas.TRIPLE_ALPHA_STRATEGY] == "TRIPLE_ALPHA"
    assert emas.STRATEGY_DISPLAY_NAMES[emas.QUAD_ALPHA_STRATEGY] == "QUAD_ALPHA"


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
        "quad",
        "quadalpha",
        "quad_status",
    } <= emas.UTBREAKOUT_CALLBACK_ACTIONS

def test_crowding_and_allocator_are_registered():
    emas = _emas_module()
    assert emas.CROWDING_UNWIND_STRATEGY in emas.UTBREAKOUT_STRATEGIES
    assert emas.STRATEGY_DISPLAY_NAMES[emas.CROWDING_UNWIND_STRATEGY] == "FUNDING_OI_CROWDING_UNWIND"
    assert {"crowd", "crowding", "crowding_status"} <= emas.UTBREAKOUT_CALLBACK_ACTIONS


def test_strategy_allocator_plan_hook_scales_once(monkeypatch):
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    engine.strategy_allocator_last_status = {}
    engine._get_utbot_filtered_breakout_config = lambda *args, **kwargs: {
        "strategy_allocator": {"minimum_samples": 1, "full_confidence_samples": 1}
    }
    engine._load_strategy_allocator_trades = lambda: [
        {"strategy": emas.QH_FLOW_STRATEGY, "net_r": -1.0, "net_pnl": -1.0}
    ]
    plan = {"strategy": emas.QH_FLOW_STRATEGY, "qty": 2.0, "risk_usdt": 1.0}
    first = engine._apply_strategy_allocator_to_plan(plan)
    second = engine._apply_strategy_allocator_to_plan(first)
    assert first["qty"] < 2.0
    assert second["qty"] == first["qty"]

def test_quad_branch_params_enable_crowding_without_cross_branch_confirmation():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    params = {
        "active_strategy": emas.QUAD_ALPHA_STRATEGY,
        "UTBotFilteredBreakoutV1": {
            "qh_flow_confirmation_enabled": True,
            "crowding_unwind_live_enabled": False,
            "crowding_unwind": {"enabled": False, "live_enabled": False},
        },
    }
    engine._crowding_unwind_runtime_config = lambda cfg=None: {
        "enabled": False,
        "live_enabled": False,
    }

    crowd = engine._quad_alpha_strategy_params(params, emas.CROWDING_UNWIND_STRATEGY)

    assert crowd["active_strategy"] == emas.CROWDING_UNWIND_STRATEGY
    cfg = crowd["UTBotFilteredBreakoutV1"]
    assert cfg["crowding_unwind_live_enabled"] is True
    assert cfg["crowding_unwind"]["enabled"] is True
    assert cfg["crowding_unwind"]["live_enabled"] is True
    assert cfg["qh_flow_confirmation_enabled"] is False


def test_quad_four_way_agreement_selects_full_risk_plan():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    engine.quad_alpha_last_status = {}
    engine.last_entry_reason = {}
    selected_plans = []
    plans = {
        emas.ENTRY_STRATEGY_UT_BREAKOUT: {"strategy": emas.ENTRY_STRATEGY_UT_BREAKOUT, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
        emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND: {"strategy": emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
        emas.QH_FLOW_STRATEGY: {"strategy": emas.QH_FLOW_STRATEGY, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
        emas.CROWDING_UNWIND_STRATEGY: {"strategy": emas.CROWDING_UNWIND_STRATEGY, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
    }
    current = {"key": None}

    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._clear_utbot_filtered_breakout_entry_plan = lambda symbol: None
    engine._get_utbot_filtered_breakout_config = lambda params=None: {}
    engine._qh_flow_runtime_config = lambda cfg=None: {}
    engine._quad_alpha_strategy_params = lambda params, branch: {"branch": branch}
    engine._utbreakout_diag_for_symbol = lambda symbol: {}
    engine._get_utbot_filtered_breakout_entry_plan = lambda symbol, side=None: plans[current["key"]]
    engine._dual_alpha_score = lambda key, side, status, plan: {
        emas.ENTRY_STRATEGY_UT_BREAKOUT: 90,
        emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND: 80,
        emas.QH_FLOW_STRATEGY: 70,
        emas.CROWDING_UNWIND_STRATEGY: 60,
    }[key]
    engine._dual_alpha_scale_plan = lambda plan, multiplier: {
        **plan,
        "qty": plan["qty"] * multiplier,
        "risk_usdt": plan["risk_usdt"] * multiplier,
    }
    engine._dual_alpha_light = lambda status, label: {
        "light": "green",
        "side": status.get("accepted_side"),
        "reason": status.get("reason"),
    }
    engine._set_utbot_filtered_breakout_entry_plan = lambda symbol, plan: selected_plans.append((symbol, dict(plan)))
    engine._store_utbot_filtered_breakout_status = lambda symbol, status: None

    async def ut(*args, **kwargs):
        current["key"] = emas.ENTRY_STRATEGY_UT_BREAKOUT
        return "long", "ut long", {"accepted_side": "long", "reason": "ut long"}

    async def rsp(*args, **kwargs):
        current["key"] = emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
        return "long", "rsp long", {"accepted_side": "long", "reason": "rsp long"}

    async def qh(*args, **kwargs):
        current["key"] = emas.QH_FLOW_STRATEGY
        return "long", "qh long", {"accepted_side": "long", "reason": "qh long"}

    async def crowd(*args, **kwargs):
        current["key"] = emas.CROWDING_UNWIND_STRATEGY
        return "long", "crowd long", {"accepted_side": "long", "reason": "crowd long"}

    engine._calculate_utbot_filtered_breakout_signal = ut
    engine._calculate_relative_strength_pullback_signal = rsp
    engine._calculate_qh_flow_signal = qh
    engine._calculate_crowding_unwind_signal = crowd

    side, _, status = asyncio.run(
        engine._calculate_quad_alpha_signal(
            "BTC/USDT:USDT",
            None,
            {"active_strategy": emas.QUAD_ALPHA_STRATEGY},
        )
    )

    assert side == "long"
    assert status["quad_alpha"]["confirmation_count"] == 4
    assert status["quad_alpha"]["agreement_state"] == "quad"
    assert status["quad_alpha"]["agreement_risk_multiplier"] == pytest.approx(1.0)
    assert selected_plans[-1][1]["quad_alpha_confirmation_count"] == 4
    assert selected_plans[-1][1]["qty"] == pytest.approx(1.0)



def test_quad_status_text_shows_four_traffic_lights_and_details():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    symbol = "ALLO/USDT:USDT"
    engine.current_utbreakout_candidate_symbol = symbol
    engine._canonical_futures_symbol = lambda value: value
    engine.quad_alpha_last_status = {
        symbol: {
            "reason": "QUAD_ALPHA waiting (none)",
            "quad_alpha": {
                "agreement_state": "none",
                "agreement_risk_multiplier": 0.0,
                "confirmation_count": 0,
                "selected_label": None,
                "selected_side": None,
                "utbreak": {
                    "light": "red",
                    "side": "short",
                    "reason": "REJECTED_L2_STRESSED",
                },
                "rspt": {
                    "light": "yellow",
                    "side": None,
                    "reason": "trend_filter_failed",
                },
                "qh_flow": {
                    "light": "yellow",
                    "side": None,
                    "reason": "QH signal window expired",
                },
                "crowding_unwind": {
                    "light": "yellow",
                    "side": None,
                    "reason": "crowding_not_extreme",
                },
            },
        }
    }

    text = asyncio.run(engine.build_quad_alpha_status_text(symbol))

    assert "🚦 전략 신호등" in text
    assert "UTBreak" in text and "🔴 SHORT 후보 거절" in text
    assert "RSPT-v3" in text and "🟡 조건 대기" in text
    assert "QH-Flow v2" in text
    assert "Crowding Unwind" in text
    assert "🟢 유효 신호: 0/4" in text
    assert "📋 전략별 상세 설명" in text
    assert "초록불만 confirmations에 포함" in text
    assert "범례: 🟢 유효 신호" in text
