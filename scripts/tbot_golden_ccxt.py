#!/usr/bin/env python3
"""Golden data for the Rust ccxt-shape layer: raw Binance USD-M payloads and
what ccxt (the library the Python bot uses) turns them into.

    python scripts/tbot_golden_ccxt.py tbot/tests/golden_ccxt.json

Public payloads come from a saved live sample (tbot/tests/fixtures); private
ones (orders, positions, account) follow the Binance API docs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import ccxt

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tbot" / "tests" / "fixtures" / "binance_public_sample.json"


def _order(**overrides):
    base = {
        "orderId": 283194212, "symbol": "SOLUSDT", "status": "FILLED",
        "clientOrderId": "bmx-SOLUSDT-l-entr-1a2b3c", "price": "0", "avgPrice": "96.7100",
        "origQty": "1.50", "executedQty": "1.50", "cumQuote": "145.06500", "timeInForce": "GTC",
        "type": "MARKET", "reduceOnly": False, "closePosition": False, "side": "BUY",
        "positionSide": "BOTH", "stopPrice": "0", "workingType": "CONTRACT_PRICE",
        "priceProtect": False, "origType": "MARKET", "priceMatch": "NONE",
        "selfTradePreventionMode": "EXPIRE_MAKER", "goodTillDate": 0,
        "time": 1793322243123, "updateTime": 1793322243150,
    }
    base.update(overrides)
    return base


ORDERS = [
    _order(),
    _order(status="NEW", executedQty="0", avgPrice="0.0000", cumQuote="0"),
    _order(status="PARTIALLY_FILLED", executedQty="0.70", cumQuote="67.69700"),
    _order(status="CANCELED", executedQty="0.20", avgPrice="96.6000", cumQuote="19.32"),
    _order(status="EXPIRED", executedQty="0", avgPrice="0.0000", cumQuote="0"),
    _order(status="REJECTED", executedQty="0", avgPrice="0.0000", cumQuote="0"),
    _order(side="SELL", reduceOnly=True, clientOrderId="bmx-SOLUSDT-s-clos-9z8y7x",
           origQty="1.50", executedQty="1.50", avgPrice="98.1200"),
    _order(type="LIMIT", origType="LIMIT", price="95.5000", status="NEW",
           executedQty="0", avgPrice="0.0000", cumQuote="0"),
    # new_order with newOrderRespType=ACK has no fill fields at all
    {"orderId": 283194999, "symbol": "BTCUSDT", "status": "NEW", "clientOrderId": "ack-only-1",
     "side": "SELL", "type": "MARKET", "origQty": "0.002", "updateTime": 1793322299000},
]


def _position(**overrides):
    base = {
        "symbol": "SOLUSDT", "positionSide": "BOTH", "positionAmt": "1.50",
        "entryPrice": "96.71", "breakEvenPrice": "96.7487", "markPrice": "97.20000000",
        "unRealizedProfit": "0.73500000", "liquidationPrice": "65.10839695",
        "isolatedMargin": "48.59", "notional": "145.80000000", "marginAsset": "USDT",
        "isolatedWallet": "48.355", "initialMargin": "48.60000000", "maintMargin": "0.72900000",
        "positionInitialMargin": "48.60000000", "openOrderInitialMargin": "0",
        "adl": 2, "bidNotional": "0", "askNotional": "0", "updateTime": 1793322243150,
    }
    base.update(overrides)
    return base


POSITIONS = [
    _position(),
    _position(positionAmt="-0.004", symbol="BTCUSDT", entryPrice="72000.1", markPrice="71800.5",
              notional="-287.202", unRealizedProfit="0.79840000", liquidationPrice="95000.2"),
    _position(positionAmt="0.000", entryPrice="0.0", notional="0", unRealizedProfit="0.00000000",
              liquidationPrice="0", isolatedMargin="0", isolatedWallet="0"),
    _position(symbol="1000PEPEUSDT", positionAmt="12000", entryPrice="0.0091234",
              markPrice="0.0091500", notional="109.8"),
]

ACCOUNT = {
    "totalInitialMargin": "48.60000000", "totalMaintMargin": "0.72900000",
    "totalWalletBalance": "210.25000000", "totalUnrealizedProfit": "0.73500000",
    "totalMarginBalance": "210.98500000", "totalPositionInitialMargin": "48.60000000",
    "totalOpenOrderInitialMargin": "0.00000000", "totalCrossWalletBalance": "161.89500000",
    "totalCrossUnPnl": "0.00000000", "availableBalance": "161.89500000",
    "maxWithdrawAmount": "161.89500000",
    "assets": [
        {"asset": "USDT", "walletBalance": "210.25000000", "unrealizedProfit": "0.73500000",
         "marginBalance": "210.98500000", "maintMargin": "0.72900000",
         "initialMargin": "48.60000000", "positionInitialMargin": "48.60000000",
         "openOrderInitialMargin": "0.00000000", "crossWalletBalance": "161.89500000",
         "crossUnPnl": "0.00000000", "availableBalance": "161.89500000",
         "maxWithdrawAmount": "161.89500000", "updateTime": 1793322243150},
        {"asset": "USDC", "walletBalance": "0.00000000", "unrealizedProfit": "0.00000000",
         "marginBalance": "0.00000000", "maintMargin": "0.00000000", "initialMargin": "0.00000000",
         "positionInitialMargin": "0.00000000", "openOrderInitialMargin": "0.00000000",
         "crossWalletBalance": "0.00000000", "crossUnPnl": "0.00000000",
         "availableBalance": "0.00000000", "maxWithdrawAmount": "0.00000000", "updateTime": 0},
    ],
    "positions": [],
}


def _clean(value):
    """JSON-safe copy (ccxt results contain only JSON types, but be strict)."""
    return json.loads(json.dumps(value, allow_nan=False))


def main(out_path):
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    ex = ccxt.binance({"options": {"defaultType": "future"}})
    raw_markets = sample["exchangeInfo"]["symbols"]
    markets = [ex.parse_market(s) for s in raw_markets]
    ex.set_markets(markets)

    def market_for(market_id):
        return ex.safe_market(market_id, None, None, "swap")

    cases = {
        "markets": [{"raw": raw, "expect": _clean(m)} for raw, m in zip(raw_markets, markets)],
        "tickers": [
            {"raw": t, "expect": _clean(ex.parse_ticker(t, market_for(t["symbol"])))}
            for t in sample["tickers"]
        ],
        "orders": [{"raw": o, "expect": _clean(ex.parse_order(o, market_for(o["symbol"])))} for o in ORDERS],
        "positions": [
            {"raw": p, "expect": _clean(ex.parse_position_risk(p, market_for(p["symbol"])))}
            for p in POSITIONS
        ],
        "balance": {"raw": ACCOUNT, "expect": _clean(ex.parse_balance_custom(ACCOUNT, "linear"))},
    }
    Path(out_path).write_text(json.dumps(cases, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tbot/tests/golden_ccxt.json")
