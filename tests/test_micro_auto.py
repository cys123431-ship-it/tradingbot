from utbreakout.micro_auto import (
    assess_micro_market_feasibility,
    build_micro_entry_plan,
    choose_micro_leverage,
    default_micro_auto_config,
)


def test_micro_auto_rejects_btc_like_min_notional_for_ten_usdt_account():
    cfg = default_micro_auto_config()

    result = assess_micro_market_feasibility(
        symbol="BTC/USDT",
        total_equity_usdt=10.0,
        free_usdt=10.0,
        min_notional_usdt=50.0,
        cfg=cfg,
        assumed_stop_distance_pct=1.0,
    )

    assert result["accepted"] is False
    assert result["reject_code"] == "REJECTED_MICRO_MIN_NOTIONAL_MARGIN"
    assert result["last_attempt"]["leverage"] == 10
    assert result["last_attempt"]["required_margin"] > result["last_attempt"]["margin_cap"]


def test_micro_auto_accepts_five_usdt_min_notional_with_lowest_safe_leverage():
    cfg = default_micro_auto_config()

    result = assess_micro_market_feasibility(
        symbol="SOL/USDT",
        total_equity_usdt=10.0,
        free_usdt=10.0,
        min_notional_usdt=5.0,
        cfg=cfg,
        assumed_stop_distance_pct=1.0,
    )

    assert result["accepted"] is True
    assert result["leverage"] == 3
    assert result["required_margin"] <= 10.0 * 0.45


def test_micro_auto_rejects_when_min_notional_bump_exceeds_risk_budget():
    cfg = default_micro_auto_config()

    plan = build_micro_entry_plan(
        side="long",
        entry_price=1.0,
        atr_value=0.05,
        min_notional_usdt=5.0,
        total_equity_usdt=10.0,
        free_usdt=10.0,
        cfg=cfg,
        selected_set={"id": 22, "name": "UT + Donchian 20", "family": "Breakout"},
        selected_timeframe="30m",
    )

    assert plan["accepted"] is False
    assert plan["reject_code"] == "REJECTED_MICRO_MIN_NOTIONAL_RISK"


def test_micro_auto_builds_plan_from_atr_risk_and_min_notional():
    cfg = default_micro_auto_config()

    plan = build_micro_entry_plan(
        side="short",
        entry_price=1.0,
        atr_value=0.01,
        min_notional_usdt=5.0,
        total_equity_usdt=10.0,
        free_usdt=10.0,
        cfg=cfg,
        selected_set={"id": 27, "name": "UT + EMA pullback", "family": "Pullback/Continuation"},
        selected_timeframe="1h",
    )

    assert plan["accepted"] is True
    assert plan["micro_auto"] is True
    assert plan["leverage"] == 3
    assert plan["planned_notional"] >= 5.0
    assert plan["planned_margin"] <= plan["margin_cap_usdt"]
    assert plan["risk_usdt"] <= cfg["max_risk_usdt"]
    assert plan["rr_multiple"] >= 2.2
    assert plan["stop_loss"] > plan["entry_price"]
    assert plan["take_profit"] < plan["entry_price"]


def test_micro_auto_fee_burden_blocks_too_tight_risk():
    cfg = default_micro_auto_config()
    cfg["max_fee_burden_pct"] = 5.0

    plan = build_micro_entry_plan(
        side="long",
        entry_price=1.0,
        atr_value=0.01,
        min_notional_usdt=5.0,
        total_equity_usdt=10.0,
        free_usdt=10.0,
        cfg=cfg,
        selected_set={"id": 22, "name": "UT + Donchian 20", "family": "Breakout"},
        selected_timeframe="30m",
    )

    assert plan["accepted"] is False
    assert plan["reject_code"] == "REJECTED_MICRO_FEE_BURDEN"


def test_micro_auto_leverage_respects_liquidation_proxy():
    cfg = default_micro_auto_config()
    cfg["min_leverage"] = 8
    cfg["max_leverage"] = 10

    result = choose_micro_leverage(
        total_equity_usdt=10.0,
        free_usdt=10.0,
        min_notional_usdt=5.0,
        stop_distance_pct=4.0,
        cfg=cfg,
    )

    assert result["accepted"] is False
    assert all(attempt["liquidation_proxy_ok"] is False for attempt in result["attempts"])
