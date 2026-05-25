from utbreakout.portfolio import evaluate_portfolio_risk


def test_portfolio_blocks_when_total_risk_exceeds_limit():
    result = evaluate_portfolio_risk(
        {"symbol": "SOL/USDT", "side": "long", "risk_usdt": 40},
        [{"symbol": "BTC/USDT", "side": "long", "risk_usdt": 70}],
        cfg={"max_total_open_risk_usdt": 100},
    )

    assert result["allowed"] is False
    assert "TOTAL_RISK_LIMIT" in result["reasons"]


def test_portfolio_blocks_excess_same_direction_positions():
    result = evaluate_portfolio_risk(
        {"symbol": "SOL/USDT", "side": "long", "risk_usdt": 10},
        [
            {"symbol": "BTC/USDT", "side": "long", "risk_usdt": 10},
            {"symbol": "ETH/USDT", "side": "long", "risk_usdt": 10},
        ],
        cfg={"max_same_direction_positions": 2},
    )

    assert "SAME_DIRECTION_LIMIT" in result["reasons"]


def test_portfolio_blocks_duplicate_symbol():
    result = evaluate_portfolio_risk(
        {"symbol": "BTC/USDT:USDT", "side": "short", "risk_usdt": 5},
        [{"symbol": "BTC/USDT", "side": "long", "risk_usdt": 10}],
    )

    assert "DUPLICATE_SYMBOL" in result["reasons"]


def test_portfolio_blocks_daily_loss_limit():
    result = evaluate_portfolio_risk(
        {"symbol": "SOL/USDT", "side": "long", "risk_usdt": 5},
        [],
        {"daily_pnl_usdt": -55},
        cfg={"daily_loss_limit_usdt": 50},
    )

    assert "DAILY_LOSS_LIMIT" in result["reasons"]


def test_portfolio_allows_clean_candidate():
    result = evaluate_portfolio_risk(
        {"symbol": "SOL/USDT", "side": "short", "risk_usdt": 5, "sector": "l1"},
        [{"symbol": "BTC/USDT", "side": "long", "risk_usdt": 10, "sector": "majors"}],
        {"daily_pnl_usdt": 5, "consecutive_losses": 0},
        cfg={"max_total_open_risk_usdt": 100, "max_same_direction_positions": 2},
    )

    assert result["allowed"] is True
    assert result["risk_multiplier"] == 1.0
