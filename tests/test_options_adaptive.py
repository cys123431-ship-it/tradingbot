import asyncio
import math
import time
from pathlib import Path

from options_trading import OptionsTradingService
from options_trading.config import OPTIONS_CAPITAL_LIMIT_USDT, normalize_options_config
from options_trading.strategy import (
    derive_dynamic_contract_targets,
    evaluate_low_iv_squeeze,
    score_option_contract,
)


def _trend_rows(count, *, start=100.0, slope=0.35, interval_ms=3_600_000):
    rows = []
    for i in range(count):
        close = start + slope * i + ((i % 5) - 2) * 0.04
        rows.append([1_700_000_000_000 + i * interval_ms, close - 0.1, close + 0.4, close - 0.4, close, 1000.0])
    return rows


def _squeeze_rows(count=100):
    rows = []
    for i in range(count):
        if i < 75:
            close = 100.0 + math.sin(i * 0.7) * 1.2
            high, low, volume = close + 1.2, close - 1.2, 100.0
        elif i < count - 1:
            close = 100.0 + math.sin(i * 0.6) * 0.08
            high, low, volume = close + 0.12, close - 0.12, 90.0
        else:
            close, high, low, volume = 100.42, 100.50, 100.15, 180.0
        rows.append([1_700_000_000_000 + i * 3_600_000, close - 0.02, high, low, close, volume])
    return rows


def _score(*, delta=0.45, iv=0.60, peer_iv=0.60, amount=1000, bid=4.8, ask=5.0, signal=None):
    return score_option_contract(
        {"symbol": "ETH-X-C", "side": "CALL", "strikePrice": "100", "dte_days": 7.0, "target_dte_days": 7.0, "target_delta": 0.45, "min_qty": 0.01},
        [{"delta": str(delta), "markIV": str(iv), "gamma": "0.02", "theta": "-0.1", "vega": "0.4"}],
        [{"amount": str(amount), "bidPrice": str(bid), "askPrice": str(ask)}],
        {"bids": [[str(bid), "10"]], "asks": [[str(ask), "10"]]},
        signal or {"score": 0.70, "spot_price": 100.0, "realized_volatility": 0.50, "forecast_volatility": 0.50, "strategy": "ADAPTIVE_TREND"},
        normalize_options_config({"min_quote_volume_usdt": 50}),
        skew_mark_payload=[{"markIV": str(peer_iv)}],
    )


def test_extreme_low_delta_lottery_is_blocked():
    result = _score(delta=0.08)
    assert result["accepted"] is False
    assert result["reason"] == "OPTION_DELTA_OUTSIDE_TARGET"


def test_low_iv_squeeze_needs_compression_breakout_and_confirmation():
    result = evaluate_low_iv_squeeze(
        _squeeze_rows(),
        _trend_rows(60, slope=0.03, interval_ms=14_400_000),
        normalize_options_config({}),
    )
    assert result["accepted"] is True
    assert result["strategy"] == "LOW_IV_SQUEEZE"
    assert result["direction"] == "CALL"
    assert result["components"]["compression_ratio"] < 0.78
    assert result["components"]["volume_ratio"] >= 1.15


def test_dynamic_dte_and_delta_change_with_signal_character():
    cfg = normalize_options_config({})
    breakout = derive_dynamic_contract_targets({"strategy": "LOW_IV_SQUEEZE", "score": 0.82}, cfg)
    persistent = derive_dynamic_contract_targets(
        {"strategy": "ADAPTIVE_TREND", "score": 0.60, "components": {"slow_timeframe": 0.8, "medium": 0.7, "breakout": 0.2}},
        cfg,
    )
    assert 2.0 <= breakout["target_dte_days"] < persistent["target_dte_days"] <= 21.0
    assert 0.35 <= breakout["target_delta"] <= 0.55
    assert 0.35 <= persistent["target_delta"] <= 0.55


def test_iv_rv_soft_penalty_then_hard_ceiling_and_squeeze_is_stricter():
    normal = _score(iv=0.60, peer_iv=0.60)
    expensive = _score(iv=0.90, peer_iv=0.90)
    too_expensive = _score(iv=1.30, peer_iv=1.30)
    squeeze = _score(
        iv=0.80,
        peer_iv=0.80,
        signal={"score": 0.75, "realized_volatility": 0.50, "strategy": "LOW_IV_SQUEEZE"},
    )
    assert normal["accepted"] and expensive["accepted"]
    assert expensive["score"] < normal["score"]
    assert too_expensive["reason"] == "OPTION_IV_TOO_EXPENSIVE"
    assert squeeze["reason"] == "OPTION_IV_TOO_EXPENSIVE"


def test_skew_penalizes_expensive_directional_option():
    expensive_direction = _score(iv=0.60, peer_iv=0.45)
    reasonable_direction = _score(iv=0.60, peer_iv=0.85)
    assert expensive_direction["skew_ratio"] > 0
    assert reasonable_direction["skew_ratio"] < 0
    assert reasonable_direction["score"] > expensive_direction["score"]


def test_spread_and_liquidity_remain_hard_gates():
    assert _score(bid=7.0, ask=10.0)["reason"] == "OPTION_SPREAD_TOO_WIDE"
    assert _score(amount=1.0)["reason"] == "OPTION_VOLUME_TOO_LOW"


class _Market:
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        if timeframe == "4h":
            return _trend_rows(160, slope=1.2, interval_ms=14_400_000)
        return _trend_rows(240)


class _Client:
    def __init__(self, api_key="", secret_key="", timeout=10):
        self.authenticated = bool(api_key and secret_key)
        self.orders = []
        self.positions_rows = []
        self.open_order_rows = []
        self.can_trade = True

    def ping(self): return {}
    def sync_time(self): return {"serverTime": int(time.time() * 1000)}
    def margin_account(self): return {"canTrade": self.can_trade, "asset": [{"asset": "USDT", "available": "24", "equity": "24"}]}
    def positions(self, symbol=None): return list(self.positions_rows)
    def open_orders(self, symbol=None): return list(self.open_order_rows)

    def exchange_info(self):
        now = int(time.time() * 1000)
        common = {
            "expiryDate": now + 5 * 86_400_000,
            "status": "TRADING",
            "strikePrice": "185",
            "underlying": "ETHUSDT",
            "unit": 1,
            "minQty": "0.01",
            "contractType": "CRYPTO_OPTIONS",
            "underlyingType": "CRYPTO",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
            ],
        }
        return {"optionSymbols": [
            {**common, "symbol": "ETH-EXP-C", "side": "CALL"},
            {**common, "symbol": "ETH-AFF-C", "side": "CALL"},
            {**common, "symbol": "ETH-PEER-P", "side": "PUT"},
        ]}

    def mark_price(self, symbol):
        return [{"symbol": symbol, "markPrice": "19990" if "EXP" in symbol else "0.49", "markIV": "0.04", "delta": "-0.45" if symbol.endswith("-P") else "0.45", "gamma": "0.01", "theta": "-0.05", "vega": "0.2"}]

    def ticker(self, symbol):
        if "EXP" in symbol:
            return [{"amount": "1000", "bidPrice": "19980", "askPrice": "20000"}]
        return [{"amount": "1000", "bidPrice": "0.48", "askPrice": "0.50"}]

    def depth(self, symbol, limit=20):
        if "EXP" in symbol:
            return {"bids": [["19980", "10"]], "asks": [["20000", "10"]]}
        return {"bids": [["0.48", "10"]], "asks": [["0.50", "10"]]}

    def new_order(self, symbol, side, order_type, quantity, **kwargs):
        self.orders.append({"symbol": symbol, "side": side, "quantity": quantity, **kwargs})
        return {"symbol": symbol, "status": "FILLED", "executedQty": quantity, "avgPrice": kwargs.get("price"), "orderId": len(self.orders), "clientOrderId": kwargs.get("client_order_id")}

    def query_order(self, symbol, **kwargs): raise AssertionError("filled fake order should not query")
    def user_trades(self, symbol, **kwargs): return []
    def exercise_records(self, symbol=None, **kwargs): return []


def _service(tmp_path, *, enabled=True):
    clients = []
    def factory(**kwargs):
        client = _Client(**kwargs)
        clients.append(client)
        return client
    service = OptionsTradingService(
        config_getter=lambda: {"enabled": enabled, "underlyings": ["ETHUSDT"], "min_quote_volume_usdt": 1},
        credentials_getter=lambda: {"api_key": "key", "secret_key": "secret"},
        market_data_exchange=_Market(),
        state_path=tmp_path / "options_state.json",
        client_factory=factory,
    )
    return service, clients


def test_can_trade_and_open_orders_remain_fail_closed(tmp_path):
    service, _ = _service(tmp_path)
    client = service._client()
    client.can_trade = False
    result = asyncio.run(service.run_cycle(force_scan=True))
    assert result["action"] == "blocked" and client.orders == []
    assert service.state["recent_scan_outcomes"][-1] == "CAN_TRADE"

    service2, _ = _service(tmp_path / "orders")
    client2 = service2._client()
    client2.open_order_rows = [{"symbol": "ETH-MANUAL-C", "status": "NEW"}]
    result2 = asyncio.run(service2.run_cycle(force_scan=True))
    assert result2["action"] == "waiting" and client2.orders == []
    assert service2.state["recent_scan_outcomes"][-1] == "OPEN_ORDER"


def test_budget_filter_skips_unaffordable_contract_before_selection(tmp_path):
    service, clients = _service(tmp_path)
    result = asyncio.run(service.run_cycle(force_scan=True))
    assert result["action"] == "entered"
    order = clients[0].orders[0]
    assert order["symbol"] == "ETH-AFF-C"
    assert order["side"] == "BUY" and order["reduce_only"] is False
    assert service.state["active_position"]["entry_total_usdt"] <= min(24.0, OPTIONS_CAPITAL_LIMIT_USDT)
    assert service.state["last_scan_diagnostics"]["contract_rejections"].get("OPTION_NET_EXPECTED_EDGE_TOO_LOW", 0) >= 1
    assert service.state["recent_scan_outcomes"][-1] == "ORDERABLE_CANDIDATE"


def test_rejection_stats_are_bounded_and_exposed(tmp_path):
    service, _ = _service(tmp_path, enabled=False)
    service._record_scan_outcome("DIRECTION_SIGNAL")
    service._record_scan_outcome("BUDGET")
    service._record_scan_outcome("BUDGET")
    status = asyncio.run(service.status_snapshot(refresh=False))
    assert status["scan_outcomes_window"] == 3
    assert status["scan_rejection_stats"]["DIRECTION_SIGNAL"] == 1
    assert status["scan_rejection_stats"]["BUDGET"] == 2


def test_options_telegram_status_displays_rejection_stats_and_adaptive_strategy():
    source = (Path(__file__).parents[1] / "bot_runtime" / "controller_options.py").read_text(encoding="utf-8")
    assert "최근 {window}회 옵션 스캔 결과" in source
    assert "scan_rejection_stats" in source
    assert "Adaptive Convexity Trend v2" in source and "Low-IV Squeeze" in source


def test_adaptive_exit_is_additive_to_legacy_tp_sl_and_sell_exit_remains_reduce_only():
    root = Path(__file__).parents[1]
    adaptive = (root / "options_trading" / "adaptive_runtime.py").read_text(encoding="utf-8")
    base = (root / "options_trading" / "runtime.py").read_text(encoding="utf-8")
    assert "OPTION_UNDERLYING_TREND_BREAK" in adaptive
    assert "OPTION_IV_COLLAPSE_WITH_WEAK_TREND" in adaptive
    assert "OPTION_NEAR_EXPIRY_DECAY_RISK" in adaptive
    assert 'cfg.get("take_profit_pct"), 0.80' in base
    assert 'cfg.get("stop_loss_pct"), 0.45' in base
    assert '"SELL",' in base and "reduce_only=True" in base
