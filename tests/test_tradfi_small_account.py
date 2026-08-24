from datetime import datetime, timezone

from trading_safety.market_session import tradfi_primary_session_status
from utbreakout.tradfi_small_account import (
    cap_tradfi_risk_tier,
    classify_tradfi_instrument,
    evaluate_tradfi_small_account_guardrails,
)


def test_regular_session_ordinary_equity_keeps_elite_and_seven_x():
    result = evaluate_tradfi_small_account_guardrails(
        symbol="SNDK/USDT:USDT",
        side="long",
        candidate_source="trend_only",
        session_status={"open": True, "reason": "regular_session_open"},
        futures_context={"basis_pct": 0.10},
    )

    assert result["allowed"] is True
    assert result["risk_tier_ceiling"] is None
    assert result["leverage_ceiling"] == 7
    assert cap_tradfi_risk_tier("elite", result["risk_tier_ceiling"]) == "elite"


def test_weekday_extended_hours_keeps_trend_but_blocks_event_only():
    extended = {"open": False, "reason": "outside_regular_session"}
    trend = evaluate_tradfi_small_account_guardrails(
        symbol="MSTR/USDT:USDT",
        side="long",
        candidate_source="trend_only",
        session_status=extended,
        futures_context={"basis_pct": 0.10},
    )
    event = evaluate_tradfi_small_account_guardrails(
        symbol="MSTR/USDT:USDT",
        side="long",
        candidate_source="event_only",
        session_status=extended,
        futures_context={"basis_pct": 0.10},
    )

    assert trend["allowed"] is True
    assert trend["risk_tier_ceiling"] == "strong"
    assert cap_tradfi_risk_tier("elite", trend["risk_tier_ceiling"]) == "strong"
    assert event["allowed"] is False
    assert event["code"] == "REJECTED_TRADFI_EXTENDED_EVENT_ONLY"


def test_weekend_and_holiday_fixed_index_risk_blocks_new_entry():
    for reason in ("weekend", "holiday"):
        result = evaluate_tradfi_small_account_guardrails(
            symbol="AMD/USDT:USDT",
            side="short",
            candidate_source="trend_only",
            session_status={"open": False, "reason": reason},
            futures_context={"basis_pct": 0.0},
        )
        assert result["allowed"] is False
        assert result["code"] == "REJECTED_TRADFI_UNDERLYING_CLOSED"


def test_adverse_basis_caps_then_blocks_but_favorable_basis_does_not():
    regular = {"open": True, "reason": "regular_session_open"}
    capped = evaluate_tradfi_small_account_guardrails(
        symbol="AMD/USDT:USDT",
        side="long",
        candidate_source="trend_only",
        session_status=regular,
        futures_context={"basis_pct": 0.90},
    )
    blocked = evaluate_tradfi_small_account_guardrails(
        symbol="AMD/USDT:USDT",
        side="long",
        candidate_source="trend_only",
        session_status=regular,
        futures_context={"basis_pct": 1.60},
    )
    favorable_short = evaluate_tradfi_small_account_guardrails(
        symbol="AMD/USDT:USDT",
        side="short",
        candidate_source="trend_only",
        session_status=regular,
        futures_context={"basis_pct": 1.60},
    )

    assert capped["allowed"] is True
    assert capped["risk_tier_ceiling"] == "base"
    assert blocked["allowed"] is False
    assert blocked["code"] == "REJECTED_TRADFI_BASIS_DISLOCATION"
    assert favorable_short["allowed"] is True


def test_geared_etp_futures_leverage_is_capped_at_four_x():
    for symbol, factor in (
        ("SOXL/USDT:USDT", 3.0),
        ("SQQQ/USDT:USDT", -3.0),
        ("KORU/USDT:USDT", 3.0),
        ("UVXY/USDT:USDT", 1.5),
    ):
        profile = classify_tradfi_instrument(symbol, "EQUITY")
        assert profile["geared_etp"] is True
        assert profile["embedded_daily_leverage"] == factor
        assert profile["small_account_leverage_ceiling"] == 4


def test_premarket_contract_is_rejected_even_during_session():
    result = evaluate_tradfi_small_account_guardrails(
        symbol="OPENAI/USDT:USDT",
        side="long",
        candidate_source="trend_only",
        session_status={"open": True, "reason": "regular_session_open"},
        underlying_type="PREMARKET",
    )

    assert result["allowed"] is False
    assert result["code"] == "REJECTED_TRADFI_PREMARKET_CONTRACT"


def test_primary_session_clock_matches_underlying_region():
    # Monday 00:30 UTC = 09:30 KST and 08:30 Hong Kong time.
    now = datetime(2026, 8, 24, 0, 30, tzinfo=timezone.utc)
    korea = tradfi_primary_session_status("KR_EQUITY", now)
    hong_kong = tradfi_primary_session_status("HK_EQUITY", now)
    united_states = tradfi_primary_session_status("EQUITY", now)

    assert korea["open"] is True
    assert korea["market_region"] == "KR"
    assert hong_kong["open"] is False
    assert hong_kong["market_region"] == "HK"
    assert united_states["open"] is False
    assert united_states["market_region"] == "US"
