import asyncio
from types import SimpleNamespace

import pytest

from options_trading import OptionsTradingService
from options_trading.config import normalize_options_config
from options_trading.strategy import (
    evaluate_option_flow,
    expected_option_net_edge,
    score_option_contract,
)


def _candidate_score(**overrides):
    contract = {
        "symbol": "BTC-X-C",
        "side": "CALL",
        "strikePrice": "100",
        "dte_days": 7.0,
        "target_dte_days": 7.0,
        "target_delta": 0.45,
        "tick_size": 0.1,
        "min_qty": 0.01,
    }
    kwargs = {
        "contract": contract,
        "mark_payload": [{"delta": "0.45", "markIV": "0.50"}],
        "ticker_payload": [{"amount": "1000", "bidPrice": "4.8", "askPrice": "5.0"}],
        "depth_payload": {"bids": [["4.8", "10"]], "asks": [["5.0", "10"]]},
        "signal": {
            "score": 0.80,
            "spot_price": 100.0,
            "realized_volatility": 0.60,
            "forecast_volatility": 0.60,
            "strategy": "ADAPTIVE_TREND",
        },
        "cfg": normalize_options_config({"min_quote_volume_usdt": 1}),
    }
    kwargs.update(overrides)
    return score_option_contract(**kwargs)


def test_net_edge_includes_premium_fees_and_exit_friction():
    cheap = expected_option_net_edge(
        side="CALL",
        spot_price=100,
        strike_price=100,
        dte_days=7,
        option_price=4,
        forecast_volatility=0.60,
        signal_score=0.80,
        spread=0.20,
    )
    expensive = expected_option_net_edge(
        side="CALL",
        spot_price=100,
        strike_price=100,
        dte_days=7,
        option_price=10,
        forecast_volatility=0.60,
        signal_score=0.80,
        spread=0.20,
    )
    assert cheap["net_expected_edge_pct"] > 0
    assert expensive["net_expected_edge_pct"] < cheap["net_expected_edge_pct"]
    assert cheap["round_trip_cost"] > 0


def test_v1_defaults_migrate_to_v2_but_operator_override_is_preserved():
    migrated = normalize_options_config(
        {
            "strategy_profile_version": "adaptive_convexity_trend_v2",
            "take_profit_pct": 0.80,
            "stop_loss_pct": 0.45,
            "trail_activation_pct": 0.35,
            "trail_drawdown_pct": 0.25,
            "underlyings": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
        }
    )
    assert migrated["strategy_profile_version"] == "adaptive_convexity_trend_v2"
    assert migrated["take_profit_pct"] == pytest.approx(3.0)
    assert migrated["stop_loss_pct"] == pytest.approx(0.55)
    assert "XRPUSDT" in migrated["underlyings"]
    assert migrated["adaptive_convexity_v2_migration_complete"] is True

    custom = normalize_options_config({"stop_loss_pct": 0.35})
    assert custom["stop_loss_pct"] == pytest.approx(0.35)
    customized_after_migration = normalize_options_config(
        {
            **migrated,
            "stop_loss_pct": 0.45,
            "underlyings": ["BTCUSDT"],
        }
    )
    assert customized_after_migration["stop_loss_pct"] == pytest.approx(0.45)
    assert customized_after_migration["underlyings"] == ["BTCUSDT"]


def test_surface_outlier_and_strong_sell_flow_are_hard_rejections():
    surface = _candidate_score(
        mark_payload=[{"delta": "0.45", "markIV": "0.80"}],
        surface_mark_payloads=[[{"markIV": "0.50"}], [{"markIV": "0.52"}]],
    )
    assert surface["reason"] == "OPTION_IV_SURFACE_PREMIUM_TOO_HIGH"

    recent_trades = [{"side": -1, "quoteQty": "1000000"}]
    flow = evaluate_option_flow(
        recent_trades,
        {"bids": [["4.8", "10"]], "asks": [["5.0", "10"]]},
    )
    assert flow["flow_score"] <= -0.70
    rejected = _candidate_score(recent_trades=recent_trades)
    assert rejected["reason"] == "OPTION_FLOW_STRONGLY_OPPOSED"


class _UnfilledMakerClient:
    authenticated = True

    def __init__(self, **_kwargs):
        self.orders = []
        self.cancels = []

    def new_order(self, symbol, side, order_type, quantity, **kwargs):
        self.orders.append({"symbol": symbol, "quantity": quantity, **kwargs})
        return {
            "symbol": symbol,
            "status": "NEW",
            "executedQty": "0",
            "clientOrderId": kwargs.get("client_order_id"),
        }

    def query_order(self, symbol, **kwargs):
        status = "CANCELED" if self.cancels else "NEW"
        return {"symbol": symbol, "status": status, "executedQty": "0"}

    def cancel_order(self, symbol, **kwargs):
        self.cancels.append({"symbol": symbol, **kwargs})
        return {"symbol": symbol, "status": "CANCELED", "executedQty": "0"}


def test_unfilled_maker_is_canceled_and_does_not_consume_signal(tmp_path, monkeypatch):
    client = _UnfilledMakerClient()

    async def no_sleep(_seconds):
        return None

    import options_trading.runtime as base_runtime

    monkeypatch.setattr(base_runtime.asyncio, "sleep", no_sleep)
    service = OptionsTradingService(
        config_getter=lambda: {
            "enabled": True,
            "maker_first_enabled": True,
            "maker_wait_seconds": 0.5,
        },
        credentials_getter=lambda: {"api_key": "key", "secret_key": "secret"},
        market_data_exchange=SimpleNamespace(),
        state_path=tmp_path / "options_state.json",
        client_factory=lambda **_kwargs: client,
    )
    selected = {
        "symbol": "BTC-X-C",
        "underlying": "BTCUSDT",
        "side": "CALL",
        "expiryDate": 2_000_000_000_000,
        "unit": 1,
        "min_qty": 0.01,
        "step_size": 0.01,
        "tick_size": 0.1,
        "bid": 0.48,
        "ask": 0.50,
        "entry_fraction": 0.70,
        "ioc_eligible": False,
        "signal": {
            "signal_key": "BTC:ADAPTIVE_TREND:CALL:1",
            "spot_price": 100.0,
            "score": 0.8,
        },
    }
    result = asyncio.run(
        service._enter(
            selected,
            {"balance": {"available": 100.0}},
        )
    )
    assert result["action"] == "waiting"
    assert client.cancels
    assert service.state.get("consumed_signal_keys") == []
    assert service.state.get("active_position") is None


@pytest.mark.parametrize(
    ("peak", "mark", "expected"),
    [(1.49, 1.0, False), (1.50, 1.09, True), (2.0, 1.34, True), (3.0, 2.09, True)],
)
def test_convex_trailing_tiers(peak, mark, expected):
    assert OptionsTradingService._adaptive_trailing_exit(1.0, peak, mark) is expected
