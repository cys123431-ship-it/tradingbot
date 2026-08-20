import asyncio
import time

import pytest

from options_trading import OptionsTradingService
from options_trading.config import OPTIONS_CAPITAL_LIMIT_USDT, normalize_options_config
from options_trading.risk import build_long_option_entry_plan
from options_trading.strategy import (
    evaluate_underlying_trend,
    score_option_contract,
    shortlist_option_contracts,
)


def _trend_rows(count, *, start=100.0, slope=0.35, interval_ms=3_600_000):
    rows = []
    for index in range(count):
        close = start + slope * index + ((index % 5) - 2) * 0.04
        rows.append(
            [
                1_700_000_000_000 + index * interval_ms,
                close - 0.1,
                close + 0.4,
                close - 0.4,
                close,
                1000.0,
            ]
        )
    return rows


def test_options_config_forces_fixed_hundred_usdt_cap_and_defaults_off():
    cfg = normalize_options_config({"enabled": True, "capital_limit_usdt": 999})
    assert cfg["enabled"] is True
    assert cfg["capital_limit_usdt"] == OPTIONS_CAPITAL_LIMIT_USDT
    assert cfg["capital_limit_usdt"] == 100.0
    assert normalize_options_config({})["enabled"] is False


def test_long_option_entry_plan_includes_fee_inside_hard_cap():
    plan = build_long_option_entry_plan(
        ask_price=12,
        index_price=2000,
        unit=1,
        min_qty=0.01,
        step_size=0.01,
        cash_bankroll_usdt=20,
        entry_fraction=0.90,
        capital_limit_usdt=500,
    )
    assert plan["accepted"] is True
    assert plan["total_entry_cost_usdt"] <= 18.0 + 1e-9
    assert plan["hard_cap_usdt"] == 20.0
    assert plan["estimated_entry_fee_usdt"] > 0


def test_long_option_entry_plan_rejects_minimum_contract_over_budget():
    plan = build_long_option_entry_plan(
        ask_price=100,
        index_price=2000,
        unit=1,
        min_qty=1,
        step_size=1,
        cash_bankroll_usdt=20,
    )
    assert plan["accepted"] is False
    assert "MINIMUM" in plan["reason"]


def test_regime_signal_uses_weighted_trend_not_all_conditions_and_selects_call():
    result = evaluate_underlying_trend(
        _trend_rows(180),
        _trend_rows(100, slope=1.1, interval_ms=14_400_000),
        {"min_abs_signal": 0.45},
    )
    assert result["accepted"] is True
    assert result["direction"] == "CALL"
    assert result["score"] > 0.45


def test_shortlist_excludes_tradfi_and_wrong_side():
    now = int(time.time() * 1000)
    common = {
        "expiryDate": now + 5 * 86_400_000,
        "status": "TRADING",
        "strikePrice": "102",
        "underlying": "ETHUSDT",
        "unit": 1,
        "minQty": "0.01",
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
        ],
    }
    info = {
        "optionSymbols": [
            {**common, "symbol": "ETH-X-C", "side": "CALL", "contractType": "CRYPTO_OPTIONS", "underlyingType": "CRYPTO"},
            {**common, "symbol": "ETH-X-P", "side": "PUT", "contractType": "CRYPTO_OPTIONS", "underlyingType": "CRYPTO"},
            {**common, "symbol": "ETH-T-C", "side": "CALL", "contractType": "TRADFI_OPTIONS", "underlyingType": "STOCK"},
        ]
    }
    rows = shortlist_option_contracts(info, "ETHUSDT", "CALL", 100, {}, now_ms=now)
    assert [row["symbol"] for row in rows] == ["ETH-X-C"]


def test_contract_score_requires_tradeable_spread_delta_and_volume():
    result = score_option_contract(
        {
            "symbol": "ETH-X-C",
            "side": "CALL",
            "strikePrice": "100",
            "dte_days": 7.0,
        },
        [{"delta": "0.45", "markIV": "0.50"}],
        [{"amount": "1000", "bidPrice": "4.8", "askPrice": "5.0"}],
        {"bids": [["4.8", "5"]], "asks": [["5.0", "5"]]},
        {
            "score": 0.8,
            "spot_price": 100.0,
            "realized_volatility": 0.6,
            "forecast_volatility": 0.6,
        },
        {},
    )
    assert result["accepted"] is True
    assert result["ask"] == 5.0


class _FakeMarket:
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        if timeframe == "4h":
            return _trend_rows(160, slope=1.2, interval_ms=14_400_000)
        return _trend_rows(240, slope=0.35)


class _FakeOptionsClient:
    def __init__(self, api_key="", secret_key="", timeout=10):
        self.authenticated = bool(api_key and secret_key)
        self.orders = []
        self.manual_positions = []
        self.trades = []

    def ping(self):
        return {}

    def sync_time(self):
        return {"serverTime": int(time.time() * 1000)}

    def margin_account(self):
        return {
            "canTrade": True,
            "asset": [{"asset": "USDT", "available": "21", "equity": "21"}],
        }

    def positions(self, symbol=None):
        return list(self.manual_positions)

    def open_orders(self, symbol=None):
        return []

    def exchange_info(self):
        now = int(time.time() * 1000)
        return {
            "optionSymbols": [
                {
                    "symbol": "ETH-TEST-185-C",
                    "expiryDate": now + 5 * 86_400_000,
                    "status": "TRADING",
                    "strikePrice": "185",
                    "underlying": "ETHUSDT",
                    "side": "CALL",
                    "unit": 1,
                    "minQty": "0.01",
                    "contractType": "CRYPTO_OPTIONS",
                    "underlyingType": "CRYPTO",
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    ],
                }
            ]
        }

    def mark_price(self, symbol):
        mark_price = "9.9" if self.manual_positions else "0.49"
        return [{"symbol": symbol, "markPrice": mark_price, "markIV": "0.02", "delta": "0.45"}]

    def ticker(self, symbol):
        return [{"symbol": symbol, "amount": "1000", "bidPrice": "0.48", "askPrice": "0.50"}]

    def depth(self, symbol, limit=20):
        return {"bids": [["0.48", "10"]], "asks": [["0.50", "10"]]}

    def new_order(self, symbol, side, order_type, quantity, **kwargs):
        self.orders.append({"symbol": symbol, "side": side, "quantity": quantity, **kwargs})
        return {
            "symbol": symbol,
            "status": "FILLED",
            "executedQty": quantity,
            "avgPrice": kwargs.get("price"),
            "orderId": 123,
            "clientOrderId": kwargs.get("client_order_id"),
        }

    def query_order(self, symbol, **kwargs):
        raise AssertionError("filled fake orders should not require a query")

    def user_trades(self, symbol, **kwargs):
        return list(self.trades)

    def exercise_records(self, symbol=None, **kwargs):
        return []


def _service(tmp_path, *, enabled=True):
    clients = []

    def factory(**kwargs):
        client = _FakeOptionsClient(**kwargs)
        clients.append(client)
        return client

    service = OptionsTradingService(
        config_getter=lambda: {
            "enabled": enabled,
            "underlyings": ["ETHUSDT"],
            "min_abs_signal": 0.45,
            "min_quote_volume_usdt": 1,
        },
        credentials_getter=lambda: {"api_key": "key", "secret_key": "secret"},
        market_data_exchange=_FakeMarket(),
        state_path=tmp_path / "options_state.json",
        client_factory=factory,
    )
    return service, clients


def test_options_runtime_defaults_to_no_order_when_disabled(tmp_path):
    service, clients = _service(tmp_path, enabled=False)
    result = asyncio.run(service.run_cycle(force_scan=True))
    assert result["action"] == "waiting"
    assert clients == []


def test_options_runtime_enters_buy_only_with_live_balance_below_hundred_cap(tmp_path):
    service, clients = _service(tmp_path, enabled=True)
    result = asyncio.run(service.run_cycle(force_scan=True))
    assert result["action"] == "entered"
    assert len(clients[0].orders) == 1
    order = clients[0].orders[0]
    assert order["side"] == "BUY"
    assert order["reduce_only"] is False
    assert order["time_in_force"] == "GTC"
    assert order["post_only"] is True
    position = service.state["active_position"]
    assert position["entry_total_usdt"] <= 21.0
    assert position["entry_total_usdt"] <= OPTIONS_CAPITAL_LIMIT_USDT
    assert 0 <= service.state["cash_bankroll_usdt"] < OPTIONS_CAPITAL_LIMIT_USDT


def test_options_runtime_blocks_entry_when_any_manual_option_position_exists(tmp_path):
    service, clients = _service(tmp_path, enabled=True)
    client = service._client()
    client.manual_positions = [{"symbol": "BTC-MANUAL-C", "quantity": "0.01"}]
    result = asyncio.run(service.run_cycle(force_scan=True))
    assert result["action"] == "waiting"
    assert client.orders == []
    assert "기존 옵션 포지션" in result["reason"]


def test_options_runtime_reconciles_manual_partial_close_once(tmp_path):
    service, _ = _service(tmp_path, enabled=False)
    client = service._client()
    symbol = "ETH-TEST-185-C"
    service.state["cash_bankroll_usdt"] = 10.0
    service.state["active_position"] = {
        "symbol": symbol,
        "side": "CALL",
        "quantity": 1.0,
        "original_quantity": 1.0,
        "entry_price": 10.0,
        "entry_total_usdt": 10.0,
        "entry_time_ms": int(time.time() * 1000),
        "expiry_date_ms": int(time.time() * 1000) + 86_400_000,
        "unit": 1.0,
        "tick_size": 0.1,
        "peak_mark": 10.0,
    }
    service._save_state()
    client.manual_positions = [{"symbol": symbol, "quantity": "0.5"}]
    client.trades = [
        {
            "id": 77,
            "orderId": 900,
            "symbol": symbol,
            "side": "SELL",
            "quantity": "0.5",
            "price": "12",
            "fee": "0.1",
        }
    ]

    result = asyncio.run(service.run_cycle(force_scan=True))

    assert result["action"] == "managed"
    assert service.state["active_position"]["quantity"] == pytest.approx(0.5)
    assert service.state["cash_bankroll_usdt"] == pytest.approx(15.9)
    assert service.state["active_position"]["partial_exit_proceeds_usdt"] == pytest.approx(6.0)
    assert service.state["active_position"]["processed_exit_trade_ids"] == ["77"]


def test_underlying_signal_uses_only_completed_candles(tmp_path):
    service, _ = _service(tmp_path, enabled=True)
    rows = _trend_rows(240)

    result = asyncio.run(
        service._underlying_signal("ETHUSDT", normalize_options_config({}))
    )

    assert result["signal_bar_ts"] == rows[-2][0]
    assert result["signal_key"].endswith(str(rows[-2][0]))


def test_main_keyboard_exposes_options_menu():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "bot_runtime" / "controller_telegram.py").read_text(
        encoding="utf-8"
    )
    assert 'KeyboardButton("/options")' in source


def test_main_runtime_runs_options_loop_without_optional_telegram_job_queue():
    from pathlib import Path

    root = Path(__file__).parents[1]
    runtime_source = (root / "bot_runtime" / "controller_exchange.py").read_text(
        encoding="utf-8"
    )
    options_source = (root / "bot_runtime" / "controller_options.py").read_text(
        encoding="utf-8"
    )
    assert "self._options_trading_loop()," in runtime_source
    assert "async def _options_trading_loop(self):" in options_source
    assert "job_queue.run_repeating" not in options_source
