import pytest

from bot_runtime.signal_breakout_analysis import SignalBreakoutAnalysisMixin
from utbreakout.opportunity_risk import (
    apply_opportunity_risk_to_plan,
    normalize_opportunity_risk_config,
    select_opportunity_risk,
)


def _plan(**overrides):
    plan = {
        "strategy": "utbot_filtered_breakout_v1",
        "side": "long",
        "entry_price": 100.0,
        "risk_distance": 1.0,
        "risk_distance_pct": 1.0,
        "atr": 1.0,
        "atr_pct": 1.0,
        "qty": 10.0,
        "risk_usdt": 10.0,
        "max_risk_per_trade_usdt": 10.0,
        "risk_per_trade_percent": 0.50,
        "planned_notional": 1_000.0,
        "planned_margin": 200.0,
        "expected_profit_usdt": 30.0,
        "quad_alpha_agreement_state": "double",
        "quad_alpha_confirmation_count": 2,
        "quad_alpha_score": 84.0,
        "strategy_allocator_multiplier": 1.0,
        "l2_state": "deep_balanced",
        "l2_risk_multiplier": 1.0,
        "l2_gate": {
            "allowed": True,
            "state": "deep_balanced",
            "risk_multiplier": 1.0,
            "direction_support": "long",
        },
    }
    plan.update(overrides)
    return plan


def test_two_strategy_alignment_selectively_increases_risk_without_changing_levels():
    source = _plan(stop_loss=99.0, take_profit=103.0)
    updated = apply_opportunity_risk_to_plan(source, daily_pnl_usdt=0.0)

    assert updated["opportunity_risk_multiplier"] == pytest.approx(1.12)
    assert updated["opportunity_risk_tier"] == "confirmed_opportunity"
    assert updated["qty"] == pytest.approx(11.2)
    assert updated["risk_usdt"] == pytest.approx(11.2)
    assert updated["risk_per_trade_percent"] == pytest.approx(0.56)
    assert updated["stop_loss"] == 99.0
    assert updated["take_profit"] == 103.0


def test_three_and_four_strategy_alignment_use_bounded_tiers():
    high = select_opportunity_risk(
        _plan(
            quad_alpha_agreement_state="triple",
            quad_alpha_confirmation_count=3,
            quad_alpha_score=88.0,
        ),
        daily_pnl_usdt=2.0,
    )
    elite = select_opportunity_risk(
        _plan(
            quad_alpha_agreement_state="quad",
            quad_alpha_confirmation_count=4,
            quad_alpha_score=93.0,
        ),
        daily_pnl_usdt=2.0,
    )

    assert high.multiplier == pytest.approx(1.25)
    assert high.tier == "high_conviction_alignment"
    assert elite.multiplier == pytest.approx(1.35)
    assert elite.tier == "elite_alignment"


def test_single_signal_and_guarded_conditions_never_expand_risk():
    single = select_opportunity_risk(
        _plan(
            quad_alpha_agreement_state="single",
            quad_alpha_confirmation_count=1,
            quad_alpha_score=99.0,
        ),
        daily_pnl_usdt=10.0,
    )
    losing_day = select_opportunity_risk(_plan(), daily_pnl_usdt=-0.01)
    allocator_reducing = select_opportunity_risk(
        _plan(strategy_allocator_multiplier=0.90),
        daily_pnl_usdt=10.0,
    )
    stressed = select_opportunity_risk(
        _plan(
            l2_state="stressed_thin",
            l2_risk_multiplier=0.0,
            l2_gate={"allowed": False, "state": "stressed_thin", "risk_multiplier": 0.0},
        ),
        daily_pnl_usdt=10.0,
    )
    volatile = select_opportunity_risk(_plan(atr_pct=5.0), daily_pnl_usdt=10.0)

    for decision in (single, losing_day, allocator_reducing, stressed, volatile):
        assert decision.multiplier == 1.0
        assert decision.tier == "baseline"


def test_account_risk_cap_limits_boost_but_never_reduces_existing_plan():
    capped = select_opportunity_risk(
        _plan(
            quad_alpha_agreement_state="quad",
            quad_alpha_confirmation_count=4,
            quad_alpha_score=95.0,
            risk_per_trade_percent=0.90,
        ),
        daily_pnl_usdt=1.0,
    )
    already_above = select_opportunity_risk(
        _plan(
            quad_alpha_agreement_state="quad",
            quad_alpha_confirmation_count=4,
            quad_alpha_score=95.0,
            risk_per_trade_percent=1.20,
        ),
        daily_pnl_usdt=1.0,
    )

    assert capped.multiplier == pytest.approx(1.0 / 0.9)
    assert already_above.multiplier == 1.0
    assert already_above.tier == "risk_cap_baseline"


def test_application_is_idempotent():
    once = apply_opportunity_risk_to_plan(_plan(), daily_pnl_usdt=0.0)
    twice = apply_opportunity_risk_to_plan(once, daily_pnl_usdt=0.0)

    assert twice["qty"] == pytest.approx(once["qty"])
    assert twice["risk_usdt"] == pytest.approx(once["risk_usdt"])


def test_config_normalization_keeps_tiers_monotonic_and_bounded():
    cfg = normalize_opportunity_risk_config(
        {
            "two_signal_multiplier": 1.30,
            "three_signal_multiplier": 1.10,
            "four_plus_signal_multiplier": 1.90,
            "max_multiplier": 1.40,
        }
    )

    assert cfg["two_signal_multiplier"] == pytest.approx(1.30)
    assert cfg["three_signal_multiplier"] == pytest.approx(1.30)
    assert cfg["four_plus_signal_multiplier"] == pytest.approx(1.40)


class _DailyStats:
    def get_daily_stats(self):
        return 0, 0.0


class _PlanStoreEngine(SignalBreakoutAnalysisMixin):
    def __init__(self):
        self.db = _DailyStats()
        self.utbot_filtered_breakout_entry_plans = {}
        self.traces = []

    def _apply_strategy_allocator_to_plan(self, plan):
        return {
            **plan,
            "strategy_allocator_applied": True,
            "strategy_allocator_multiplier": 1.0,
        }

    def get_runtime_common_settings(self):
        return {
            "leverage": 5,
            "dynamic_leverage": {"enabled": False},
            "opportunity_risk": {"enabled": True},
        }

    def _canonical_futures_symbol(self, symbol):
        return str(symbol)

    def _futures_symbol_key(self, symbol):
        return str(symbol).replace("/", "").replace(":USDT", "")

    def _utbreakout_trace_key(self, symbol):
        return self._futures_symbol_key(symbol)

    def _utbreakout_trace_event(self, symbol, stage, outcome, **payload):
        self.traces.append((symbol, stage, outcome, payload))


def test_plan_store_applies_overlay_only_after_quad_metadata_exists():
    engine = _PlanStoreEngine()
    aggregate = _plan(plan_symbol="BTC/USDT:USDT")

    engine._set_utbot_filtered_breakout_entry_plan("BTC/USDT:USDT", aggregate)
    stored = engine._get_utbot_filtered_breakout_entry_plan("BTC/USDT:USDT", "long")

    assert stored["opportunity_risk_multiplier"] == pytest.approx(1.12)
    assert stored["qty"] == pytest.approx(11.2)
    assert engine.traces[-1][3]["opportunity_risk_tier"] == "confirmed_opportunity"
