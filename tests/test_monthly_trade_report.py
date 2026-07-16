from datetime import datetime
from zoneinfo import ZoneInfo

from trading_safety.monthly_report import build_monthly_trade_report, previous_calendar_month


def test_previous_calendar_month_handles_year_boundary():
    assert previous_calendar_month(datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul"))) == (2025, 12)


def test_report_counts_trade_once_and_lists_confirmation_participants():
    report = build_monthly_trade_report([
        {
            "trade_id": "one",
            "primary_strategy": "ut",
            "confirmation_strategies": ["ut", "mtrend"],
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "entry_time": "2026-06-02T00:00:00+00:00",
            "exit_time": "2026-06-02T04:00:00+00:00",
            "entry_price": 100,
            "exit_price": 110,
            "filled_qty": 1,
            "leverage": 5,
            "gross_pnl_usdt": 10,
            "net_pnl_usdt": 9,
            "realized_r": 2,
            "exit_legs": [{"label": "TP1", "qty": 1, "realized_pnl_usdt": 10}],
        }
    ], 2026, 6, generated_at=datetime(2026, 7, 1, 9, tzinfo=ZoneInfo("Asia/Seoul")))
    assert "ALL REAL TRADES                     1" in report
    assert "ut" in report
    assert "mtrend" in report
    assert report.count("signal participants: ut, mtrend") == 1
    assert "TP1" in report
    assert "06-02 13:00       BTC/USDT:USDT" in report


def test_zero_trade_month_still_produces_report():
    report = build_monthly_trade_report([], 2026, 6)
    assert "No real positions were closed" in report
    assert "Open positions at month end" in report


def test_report_tracks_lxr_as_a_primary_live_strategy():
    report = build_monthly_trade_report([{
        "trade_id": "lxr-one",
        "primary_strategy": "liquidation_exhaustion_reversal_v1",
        "confirmation_strategies": ["liquidation_exhaustion_reversal_v1"],
        "symbol": "ETH/USDT:USDT",
        "side": "short",
        "entry_time": "2026-06-10T00:00:00+00:00",
        "exit_time": "2026-06-10T01:00:00+00:00",
        "entry_price": 100,
        "exit_price": 98,
        "filled_qty": 1,
        "gross_pnl_usdt": 2,
        "net_pnl_usdt": 1.8,
        "realized_r": 1.2,
    }], 2026, 6)
    assert "liquidation_exhaustion_reversal" in report
    assert "ALL REAL TRADES                     1" in report
