import asyncio
import time

import pytest

from utbreakout.liquidation_exhaustion_reversal import LiquidationExhaustionDecision


def _emas_module():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI")


def _async_value(value):
    async def _call(*args, **kwargs):
        return value

    return _call


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


def test_lxr_strategy_and_telegram_actions_are_registered():
    emas = _emas_module()
    assert emas.LXR_STRATEGY in emas.UTBREAKOUT_STRATEGIES
    assert emas.STRATEGY_DISPLAY_NAMES[emas.LXR_STRATEGY] == "LXR"
    assert {"lxr", "liquidation_reversal", "lxr_status"} <= emas.UTBREAKOUT_CALLBACK_ACTIONS

    engine = object.__new__(emas.SignalEngine)
    engine._liquidation_exhaustion_reversal_runtime_config = lambda cfg=None: {
        "enabled": False,
        "live_enabled": False,
    }
    params = engine._quad_alpha_strategy_params(
        {"UTBotFilteredBreakoutV1": {}},
        emas.LXR_STRATEGY,
    )
    cfg = params["UTBotFilteredBreakoutV1"]
    assert params["active_strategy"] == emas.LXR_STRATEGY
    assert cfg["liquidation_exhaustion_reversal_live_enabled"] is True
    assert cfg["liquidation_exhaustion_reversal"]["live_enabled"] is True


def test_lxr_live_signal_builds_structure_anchored_plan(monkeypatch):
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    now_ms = int(time.time() * 1000)
    ohlcv = [
        [now_ms - (60 - index) * 900_000, 100.0, 100.4, 99.6, 100.0, 100.0]
        for index in range(55)
    ]

    class MarketData:
        def fetch_ohlcv(self, symbol, timeframe, limit=220):
            assert timeframe == "15m"
            return ohlcv

    class DailyStats:
        def get_daily_stats(self):
            return 0, 0.0

        def get_daily_entry_count(self):
            return 0

    decision = LiquidationExhaustionDecision(
        side="long",
        allowed=True,
        score=82.0,
        risk_multiplier=0.35,
        reason="LXR LONG confirmed",
        metrics={
            "atr": 1.0,
            "structure_stop": 96.0,
            "shock_atr": 3.0,
            "shock_volume_ratio": 2.5,
            "open_interest_change_1h": -1.2,
            "reclaim_atr": 0.8,
        },
    )
    monkeypatch.setattr(emas, "evaluate_liquidation_exhaustion_reversal", lambda *args, **kwargs: decision)
    engine.market_data_exchange = MarketData()
    engine.db = DailyStats()
    engine.last_entry_reason = {}
    engine.liquidation_exhaustion_reversal_last_status = {}
    engine._get_utbot_filtered_breakout_config = lambda params=None: {
        "liquidation_exhaustion_reversal_live_enabled": True,
        "liquidation_exhaustion_reversal": {"enabled": True, "live_enabled": True},
        "risk_per_trade_percent": 1.0,
        "max_risk_per_trade_percent": 1.0,
    }
    engine._canonical_futures_symbol = lambda symbol: "BTC/USDT:USDT"
    engine._clear_utbot_filtered_breakout_entry_plan = lambda symbol: None
    engine._store_utbot_filtered_breakout_status = lambda symbol, status: None
    engine.is_upbit_mode = lambda: False
    engine.is_trade_direction_allowed = lambda side: True
    engine._fetch_utbreakout_futures_context = _async_value({"open_interest_change_1h": -1.2})
    engine._evaluate_shared_l2_gate = _async_value({
        "allowed": True,
        "state": "bid_support",
        "direction_support": "long",
        "risk_multiplier": 0.80,
    })
    engine._evaluate_utbreakout_market_quality = lambda side, cfg, values: {
        "state": True,
        "hard_block": False,
        "risk_multiplier": 1.0,
        "summary": "PASS",
    }
    engine.get_balance_info = _async_value((100.0, 100.0, 0.0))
    engine.get_runtime_common_settings = lambda: {"leverage": 5}
    captured = {}
    engine._set_utbot_filtered_breakout_entry_plan = lambda symbol, plan: captured.update(plan)

    side, reason, status = asyncio.run(
        engine._calculate_liquidation_exhaustion_reversal_signal(
            "BTC/USDT:USDT",
            None,
            {"active_strategy": emas.LXR_STRATEGY},
        )
    )

    assert side == "long"
    assert reason.startswith("ACCEPTED_ENTRY: LXR LONG")
    assert status["accepted_code"] == "ACCEPTED_ENTRY"
    assert captured["strategy"] == emas.LXR_STRATEGY
    assert captured["structure_stop"] == pytest.approx(96.0)
    assert captured["hard_stop_loss"] < 96.0
    assert captured["ev_time_stop_bars"] == 8
    assert captured["second_take_profit_r_multiple"] == pytest.approx(2.6)


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


def test_quad_five_way_agreement_selects_full_risk_plan():
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
        emas.LXR_STRATEGY: {"strategy": emas.LXR_STRATEGY, "plan_symbol": "BTC/USDT:USDT", "qty": 1.0, "risk_usdt": 1.0},
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
        emas.LXR_STRATEGY: 50,
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

    async def lxr(*args, **kwargs):
        current["key"] = emas.LXR_STRATEGY
        return "long", "lxr long", {"accepted_side": "long", "reason": "lxr long"}

    engine._calculate_utbot_filtered_breakout_signal = ut
    engine._calculate_relative_strength_pullback_signal = rsp
    engine._calculate_qh_flow_signal = qh
    engine._calculate_crowding_unwind_signal = crowd
    engine._calculate_liquidation_exhaustion_reversal_signal = lxr

    side, _, status = asyncio.run(
        engine._calculate_quad_alpha_signal(
            "BTC/USDT:USDT",
            None,
            {"active_strategy": emas.QUAD_ALPHA_STRATEGY},
        )
    )

    assert side == "long"
    assert status["quad_alpha"]["confirmation_count"] == 5
    assert status["quad_alpha"]["agreement_state"] == "five"
    assert status["quad_alpha"]["agreement_risk_multiplier"] == pytest.approx(1.0)
    assert selected_plans[-1][1]["quad_alpha_confirmation_count"] == 5
    assert selected_plans[-1][1]["quad_alpha_confirmation_strategies"][-1] == emas.LXR_STRATEGY
    assert selected_plans[-1][1]["qty"] == pytest.approx(1.0)


def test_quad_strategy_selector_supports_multi_select_and_stable_order():
    emas = _emas_module()
    selected = emas.normalize_quad_alpha_enabled_strategies([
        emas.LXR_STRATEGY,
        "ut",
        "ut",
        "unknown",
    ])

    assert selected == [
        emas.ENTRY_STRATEGY_UT_BREAKOUT,
        emas.LXR_STRATEGY,
    ]
    assert emas.normalize_quad_alpha_enabled_strategies(None) == list(
        emas.QUAD_ALPHA_BRANCH_ORDER
    )
    assert emas.normalize_quad_alpha_enabled_strategies([]) == []
    live_flags = emas.quad_alpha_branch_live_flags(selected)
    assert live_flags[emas.ENTRY_STRATEGY_UT_BREAKOUT] is True
    assert live_flags[emas.LXR_STRATEGY] is True
    assert live_flags[emas.ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND] is False
    assert live_flags[emas.QH_FLOW_STRATEGY] is False
    assert live_flags[emas.CROWDING_UNWIND_STRATEGY] is False

    keyboard = emas.build_quad_alpha_selection_keyboard(selected)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == 7
    assert buttons[0].text.startswith("✅ 1.")
    assert buttons[1].text.startswith("⬜ 2.")
    assert buttons[4].text.startswith("✅ 5.")
    assert buttons[0].callback_data == "utb:qsel:ut"
    assert buttons[-2].callback_data == "utb:qsel:apply"
    assert buttons[-1].callback_data == "utb:qsel:cancel"


def test_quad_disabled_branches_are_not_evaluated_or_counted():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    engine.quad_alpha_last_status = {}
    engine.last_entry_reason = {}
    selected_plans = []
    current = {"key": None}
    lxr_plan = {
        "strategy": emas.LXR_STRATEGY,
        "plan_symbol": "BTC/USDT:USDT",
        "qty": 2.0,
        "risk_usdt": 2.0,
    }

    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._clear_utbot_filtered_breakout_entry_plan = lambda symbol: None
    engine._get_utbot_filtered_breakout_config = lambda params=None: {
        "quad_alpha_enabled_strategies": [emas.LXR_STRATEGY],
        "quad_alpha_single_signal_risk_multiplier": 0.45,
    }
    engine._qh_flow_runtime_config = lambda cfg=None: {}
    engine._quad_alpha_strategy_params = lambda params, branch: {"branch": branch}
    engine._utbreakout_diag_for_symbol = lambda symbol: {}
    engine._get_utbot_filtered_breakout_entry_plan = lambda symbol, side=None: (
        lxr_plan if current["key"] == emas.LXR_STRATEGY else None
    )
    engine._dual_alpha_score = lambda key, side, status, plan: 80.0
    engine._dual_alpha_scale_plan = lambda plan, multiplier: {
        **plan,
        "qty": plan["qty"] * multiplier,
        "risk_usdt": plan["risk_usdt"] * multiplier,
    }
    engine._set_utbot_filtered_breakout_entry_plan = lambda symbol, plan: selected_plans.append(dict(plan))
    engine._store_utbot_filtered_breakout_status = lambda symbol, status: None

    async def disabled_branch_called(*args, **kwargs):
        raise AssertionError("disabled branch was evaluated")

    async def lxr(*args, **kwargs):
        current["key"] = emas.LXR_STRATEGY
        return "long", "lxr long", {
            "accepted_side": "long",
            "accepted_code": "ACCEPTED_ENTRY",
            "reason": "lxr long",
        }

    engine._calculate_utbot_filtered_breakout_signal = disabled_branch_called
    engine._calculate_relative_strength_pullback_signal = disabled_branch_called
    engine._calculate_qh_flow_signal = disabled_branch_called
    engine._calculate_crowding_unwind_signal = disabled_branch_called
    engine._calculate_liquidation_exhaustion_reversal_signal = lxr

    side, _, status = asyncio.run(
        engine._calculate_quad_alpha_signal(
            "BTC/USDT:USDT",
            None,
            {"active_strategy": emas.QUAD_ALPHA_STRATEGY},
        )
    )

    summary = status["quad_alpha"]
    assert side == "long"
    assert summary["enabled_count"] == 1
    assert summary["enabled_strategies"] == [emas.LXR_STRATEGY]
    assert summary["confirmation_count"] == 1
    assert summary["agreement_risk_multiplier"] == pytest.approx(0.45)
    assert summary["utbreak"]["light"] == "off"
    assert summary["rspt"]["light"] == "off"
    assert summary["qh_flow"]["light"] == "off"
    assert summary["crowding_unwind"]["light"] == "off"
    assert summary["lxr"]["light"] == "green"
    assert selected_plans[-1]["qty"] == pytest.approx(0.9)

    report = asyncio.run(engine.build_quad_alpha_status_text("BTC/USDT:USDT"))
    assert "Active strategies: 1/5" in report
    assert "🟢 유효 신호: 1/1" in report
    assert report.count("⚫ OFF") >= 4


def test_quad_accepts_lxr_as_the_only_signal_at_single_signal_risk():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    engine.quad_alpha_last_status = {}
    engine.last_entry_reason = {}
    selected_plans = []
    current = {"key": None}
    lxr_plan = {
        "strategy": emas.LXR_STRATEGY,
        "plan_symbol": "BTC/USDT:USDT",
        "qty": 2.0,
        "risk_usdt": 2.0,
    }

    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._clear_utbot_filtered_breakout_entry_plan = lambda symbol: None
    engine._get_utbot_filtered_breakout_config = lambda params=None: {
        "quad_alpha_single_signal_risk_multiplier": 0.45,
    }
    engine._qh_flow_runtime_config = lambda cfg=None: {}
    engine._quad_alpha_strategy_params = lambda params, branch: {"branch": branch}
    engine._utbreakout_diag_for_symbol = lambda symbol: {}
    engine._get_utbot_filtered_breakout_entry_plan = lambda symbol, side=None: (
        lxr_plan if current["key"] == emas.LXR_STRATEGY else None
    )
    engine._dual_alpha_score = lambda key, side, status, plan: 80.0
    engine._dual_alpha_scale_plan = lambda plan, multiplier: {
        **plan,
        "qty": plan["qty"] * multiplier,
        "risk_usdt": plan["risk_usdt"] * multiplier,
    }
    engine._dual_alpha_light = lambda status, label: {
        "light": "green" if (status or {}).get("accepted_side") else "yellow",
        "side": (status or {}).get("accepted_side"),
        "reason": (status or {}).get("reason"),
    }
    engine._set_utbot_filtered_breakout_entry_plan = lambda symbol, plan: selected_plans.append(dict(plan))
    engine._store_utbot_filtered_breakout_status = lambda symbol, status: None

    async def waiting(*args, **kwargs):
        return None, "waiting", {"stage": "waiting", "reason": "waiting"}

    async def lxr(*args, **kwargs):
        current["key"] = emas.LXR_STRATEGY
        return "long", "lxr long", {
            "accepted_side": "long",
            "accepted_code": "ACCEPTED_ENTRY",
            "reason": "lxr long",
        }

    engine._calculate_utbot_filtered_breakout_signal = waiting
    engine._calculate_relative_strength_pullback_signal = waiting
    engine._calculate_qh_flow_signal = waiting
    engine._calculate_crowding_unwind_signal = waiting
    engine._calculate_liquidation_exhaustion_reversal_signal = lxr

    side, _, status = asyncio.run(
        engine._calculate_quad_alpha_signal(
            "BTC/USDT:USDT",
            None,
            {"active_strategy": emas.QUAD_ALPHA_STRATEGY},
        )
    )

    assert side == "long"
    assert status["quad_alpha"]["confirmation_count"] == 1
    assert status["quad_alpha"]["agreement_state"] == "single"
    assert status["quad_alpha"]["agreement_risk_multiplier"] == pytest.approx(0.45)
    assert selected_plans[-1]["strategy"] == emas.LXR_STRATEGY
    assert selected_plans[-1]["qty"] == pytest.approx(0.9)


def test_quad_keeps_valid_branch_when_another_branch_crashes():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    engine.quad_alpha_last_status = {}
    engine.last_entry_reason = {}
    selected_plans = []
    current = {"key": None}
    lxr_plan = {
        "strategy": emas.LXR_STRATEGY,
        "plan_symbol": "BTC/USDT:USDT",
        "qty": 2.0,
        "risk_usdt": 2.0,
    }

    engine._canonical_futures_symbol = lambda symbol: symbol
    engine._clear_utbot_filtered_breakout_entry_plan = lambda symbol: None
    engine._get_utbot_filtered_breakout_config = lambda params=None: {
        "quad_alpha_single_signal_risk_multiplier": 0.45,
    }
    engine._qh_flow_runtime_config = lambda cfg=None: {}
    engine._quad_alpha_strategy_params = lambda params, branch: {"branch": branch}
    engine._utbreakout_diag_for_symbol = lambda symbol: {}
    engine._get_utbot_filtered_breakout_entry_plan = lambda symbol, side=None: (
        lxr_plan if current["key"] == emas.LXR_STRATEGY else None
    )
    engine._dual_alpha_score = lambda key, side, status, plan: 80.0
    engine._dual_alpha_scale_plan = lambda plan, multiplier: {
        **plan,
        "qty": plan["qty"] * multiplier,
        "risk_usdt": plan["risk_usdt"] * multiplier,
    }
    engine._dual_alpha_light = lambda status, label: {
        "light": "green" if (status or {}).get("accepted_side") else "yellow",
        "side": (status or {}).get("accepted_side"),
        "reason": (status or {}).get("reason"),
    }
    engine._set_utbot_filtered_breakout_entry_plan = lambda symbol, plan: selected_plans.append(dict(plan))
    engine._store_utbot_filtered_breakout_status = lambda symbol, status: None

    async def waiting(*args, **kwargs):
        return None, "waiting", {"stage": "waiting", "reason": "waiting"}

    async def broken_qh(*args, **kwargs):
        raise RuntimeError("temporary data failure")

    async def lxr(*args, **kwargs):
        current["key"] = emas.LXR_STRATEGY
        return "long", "lxr long", {
            "accepted_side": "long",
            "accepted_code": "ACCEPTED_ENTRY",
            "reason": "lxr long",
        }

    engine._calculate_utbot_filtered_breakout_signal = waiting
    engine._calculate_relative_strength_pullback_signal = waiting
    engine._calculate_qh_flow_signal = broken_qh
    engine._calculate_crowding_unwind_signal = waiting
    engine._calculate_liquidation_exhaustion_reversal_signal = lxr

    side, _, status = asyncio.run(
        engine._calculate_quad_alpha_signal(
            "BTC/USDT:USDT",
            None,
            {"active_strategy": emas.QUAD_ALPHA_STRATEGY},
        )
    )

    assert side == "long"
    assert status["quad_alpha"]["confirmation_count"] == 1
    assert status["quad_alpha"]["agreement_state"] == "single"
    assert "QH-Flow v2 unavailable: RuntimeError" in status["quad_alpha"]["qh_flow"]["reason"]
    assert selected_plans[-1]["strategy"] == emas.LXR_STRATEGY
    assert selected_plans[-1]["qty"] == pytest.approx(0.9)



def test_quad_status_text_shows_five_traffic_lights_and_details():
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
                "lxr": {
                    "light": "yellow",
                    "side": None,
                    "reason": "shock_not_extreme",
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
    assert "LXR" in text
    assert "🟢 유효 신호: 0/5" in text
    assert "📋 전략별 상세 설명" in text
    assert "초록불만 confirmations에 포함" in text
    assert "범례: 🟢 유효 신호" in text


def test_quad_status_distinguishes_crowding_data_missing_from_not_extreme():
    emas = _emas_module()
    engine = object.__new__(emas.SignalEngine)
    symbol = "PUMP/USDT:USDT"
    engine.current_utbreakout_candidate_symbol = symbol
    engine._canonical_futures_symbol = lambda value: value
    engine.quad_alpha_last_status = {
        symbol: {
            "reason": "QUAD_ALPHA waiting",
            "quad_alpha": {
                "agreement_state": "none",
                "agreement_risk_multiplier": 0.0,
                "confirmation_count": 0,
                "selected_label": None,
                "selected_side": None,
                "utbreak": {"light": "yellow", "side": None, "reason": "waiting"},
                "rspt": {"light": "yellow", "side": None, "reason": "waiting"},
                "qh_flow": {"light": "yellow", "side": None, "reason": "waiting"},
                "crowding_unwind": {
                    "light": "yellow",
                    "side": None,
                    "reason": "Crowding waiting: crowding_derivatives_data_missing",
                    "metrics": {
                        "funding_rate": None,
                        "funding_percentile": None,
                        "oi_z": None,
                        "oi_change_4h_pct": None,
                        "long_short_ratio": None,
                        "derivatives_data_ready": False,
                        "missing_derivatives_fields": [
                            "funding_rate",
                            "open_interest_delta_z|open_interest_change_4h",
                            "long_short_ratio",
                        ],
                    },
                },
                "lxr": {"light": "yellow", "side": None, "reason": "waiting"},
            },
        }
    }

    report = asyncio.run(engine.build_quad_alpha_status_text(symbol))

    assert "Crowding Unwind  ⚪ 파생데이터 누락" in report
    assert "funding=N/A" in report
    assert "OI z=N/A" in report
    assert "L/S=N/A" in report
    assert "누락 필드: funding_rate" in report
