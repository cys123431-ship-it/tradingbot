#!/usr/bin/env python3
"""Emit read-only bot/exchange snapshots as newline-delimited JSON.

The Windows Rust monitor launches this script through SSH.  It intentionally
contains no order creation, cancellation, leverage, or configuration mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

import ccxt


RUNNING = True


def _stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def _float(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _text(value, limit=400):
    return str(value or "").replace("\x00", " ").strip()[:limit]


def _canonical_symbol(value):
    return _text(value, 80).replace(":USDT", "")


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (OSError, ValueError, TypeError):
        return default


def _credentials(config, mode):
    api = config.get("api", {}) if isinstance(config.get("api"), dict) else {}
    if mode == "binance_testnet":
        return api.get("testnet", {}) or {}
    return api.get("mainnet", {}) or {}


def _exchange_mode(config):
    api = config.get("api", {}) if isinstance(config.get("api"), dict) else {}
    mode = _text(api.get("exchange_mode"), 40).lower()
    if mode not in {"binance_testnet", "binance_mainnet"}:
        mode = "binance_testnet" if api.get("use_testnet", True) else "binance_mainnet"
    return mode


def build_exchanges(config):
    mode = _exchange_mode(config)
    creds = _credentials(config, mode)
    private = ccxt.binance(
        {
            "apiKey": creds.get("api_key", ""),
            "secret": creds.get("secret_key", ""),
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "warnOnFetchOpenOrdersWithoutSymbol": False,
            },
        }
    )
    if mode == "binance_testnet":
        if hasattr(private, "enable_demo_trading"):
            private.enable_demo_trading(True)
        elif hasattr(private, "enableDemoTrading"):
            private.enableDemoTrading(True)
        else:
            private.set_sandbox_mode(True)
    public = ccxt.binance(
        {
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "warnOnFetchOpenOrdersWithoutSymbol": False,
            },
        }
    )
    return mode, private, public


def normalize_position(raw):
    raw = raw if isinstance(raw, dict) else {}
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    amount = _float(raw.get("contracts"))
    signed_amount = _float(info.get("positionAmt"))
    if amount is None:
        amount = abs(signed_amount or 0.0)
    if not amount or abs(amount) <= 0:
        return None
    side = _text(raw.get("side"), 12).lower()
    if side not in {"long", "short"}:
        side = "short" if (signed_amount or 0.0) < 0 else "long"
    entry = _float(raw.get("entryPrice"), _float(info.get("entryPrice"), 0.0)) or 0.0
    mark = _float(raw.get("markPrice"), _float(info.get("markPrice"), 0.0)) or 0.0
    notional = abs(
        _float(raw.get("notional"), _float(info.get("notional"), amount * mark)) or 0.0
    )
    leverage = _float(raw.get("leverage"), _float(info.get("leverage"), 1.0)) or 1.0
    margin = _float(raw.get("initialMargin"), _float(info.get("initialMargin")))
    if not margin:
        margin = notional / max(1.0, leverage)
    pnl = _float(raw.get("unrealizedPnl"), _float(info.get("unRealizedProfit"), 0.0)) or 0.0
    return {
        "symbol": _canonical_symbol(raw.get("symbol") or info.get("symbol")),
        "side": side.upper(),
        "contracts": abs(amount),
        "entry_price": entry,
        "mark_price": mark,
        "liquidation_price": _float(
            raw.get("liquidationPrice"), _float(info.get("liquidationPrice"))
        ),
        "leverage": leverage,
        "margin_usdt": margin,
        "notional_usdt": notional,
        "unrealized_pnl": pnl,
        "roe_percent": (pnl / margin * 100.0) if margin else None,
    }


def fetch_positions(exchange):
    positions = []
    for raw in exchange.fetch_positions() or []:
        normalized = normalize_position(raw)
        if normalized:
            positions.append(normalized)
    return positions


def _order_price(raw):
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    for value in (
        raw.get("stopPrice"),
        raw.get("triggerPrice"),
        info.get("triggerPrice"),
        info.get("stopPrice"),
        raw.get("price"),
        info.get("price"),
    ):
        price = _float(value)
        if price and price > 0:
            return price
    return None


def normalize_order(raw, source="regular"):
    raw = raw if isinstance(raw, dict) else {}
    info = raw.get("info") if isinstance(raw.get("info"), dict) else raw
    order_type = _text(raw.get("type") or info.get("orderType") or info.get("type"), 40).upper()
    reduce_only = bool(
        raw.get("reduceOnly")
        or info.get("reduceOnly")
        or _text(info.get("closePosition"), 8).lower() == "true"
    )
    return {
        "id": _text(raw.get("id") or info.get("algoId") or info.get("orderId"), 80),
        "symbol": _canonical_symbol(raw.get("symbol") or info.get("symbol")),
        "side": _text(raw.get("side") or info.get("side"), 12).upper(),
        "type": order_type,
        "price": _order_price(raw),
        "amount": _float(raw.get("amount"), _float(info.get("quantity"))),
        "reduce_only": reduce_only,
        "source": source,
    }


def fetch_orders(exchange, symbol):
    orders = []
    for raw in exchange.fetch_open_orders(symbol) or []:
        orders.append(normalize_order(raw, "regular"))
    fetch_algo = getattr(exchange, "fapiPrivateGetOpenAlgoOrders", None)
    if callable(fetch_algo):
        params = {}
        try:
            market = exchange.market(symbol)
            if market and market.get("id"):
                params["symbol"] = market["id"]
        except Exception:
            pass
        response = fetch_algo(params)
        raw_orders = response
        if isinstance(response, dict):
            raw_orders = (
                response.get("orders")
                or response.get("algoOrders")
                or response.get("rows")
                or response.get("data")
                or []
            )
        for raw in raw_orders or []:
            orders.append(normalize_order(raw, "algo"))
    return [order for order in orders if order.get("price")]


def _matching_hint(runtime, symbol):
    for hint in runtime.get("position_hints", []) if isinstance(runtime, dict) else []:
        if isinstance(hint, dict) and _canonical_symbol(hint.get("symbol")) == symbol:
            return hint
    return None


def classify_position(position, runtime):
    if not position:
        return None
    position = dict(position)
    hint = _matching_hint(runtime, position.get("symbol"))
    if hint:
        position["source"] = "BOT"
        position["strategy"] = _text(hint.get("strategy"), 80) or "BOT"
    else:
        position["source"] = "MANUAL / UNKNOWN"
        position["strategy"] = None
    return position


def normalize_candles(rows):
    candles = []
    for row in rows or []:
        if len(row) < 6:
            continue
        candles.append(
            {
                "ts": int(row[0]),
                "open": _float(row[1], 0.0),
                "high": _float(row[2], 0.0),
                "low": _float(row[3], 0.0),
                "close": _float(row[4], 0.0),
                "volume": _float(row[5], 0.0),
            }
        )
    return candles


def _fallback_symbol(runtime):
    if isinstance(runtime, dict):
        bot = runtime.get("bot") if isinstance(runtime.get("bot"), dict) else {}
        for value in (bot.get("current_symbol"), bot.get("scanner_active_symbol")):
            symbol = _canonical_symbol(value)
            if symbol:
                return symbol
    return "BTC/USDT"


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def stream(root, interval, timeframe, candle_limit):
    config = _load_json(root / "config.json", {})
    if not config:
        raise RuntimeError("config.json could not be loaded")
    mode, private, public = build_exchanges(config)
    private.load_markets()
    public.load_markets()
    runtime_path = root / "runtime" / "desktop_monitor.json"
    last_symbol = None
    last_runtime_epoch = 0

    while RUNNING:
        started = time.monotonic()
        runtime = _load_json(runtime_path, {})
        try:
            positions = fetch_positions(private)
            position = positions[0] if positions else None
            symbol = _canonical_symbol(
                position.get("symbol") if position else _fallback_symbol(runtime)
            )
            market_symbol = f"{symbol}:USDT" if symbol.endswith("/USDT") else symbol
            orders = fetch_orders(private, market_symbol) if position else []
            rows = public.fetch_ohlcv(
                market_symbol,
                timeframe=timeframe,
                limit=candle_limit if symbol != last_symbol else 2,
            )
            candles = normalize_candles(rows)
            runtime_epoch = int(runtime.get("epoch") or 0) if isinstance(runtime, dict) else 0
            payload = {
                "kind": "snapshot",
                "ts": time.time(),
                "exchange_mode": mode,
                "symbol": symbol,
                "timeframe": timeframe,
                "position": classify_position(position, runtime),
                "orders": orders,
                "runtime": runtime if runtime_epoch != last_runtime_epoch or symbol != last_symbol else None,
                "candles": candles if symbol != last_symbol else [],
                "candle": candles[-1] if candles else None,
                "error": None,
            }
            _emit(payload)
            last_symbol = symbol
            last_runtime_epoch = runtime_epoch
        except Exception as exc:
            _emit(
                {
                    "kind": "snapshot",
                    "ts": time.time(),
                    "exchange_mode": mode,
                    "symbol": last_symbol,
                    "error": _text(f"{type(exc).__name__}: {exc}", 500),
                }
            )
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + max(0.2, interval - elapsed)
        while RUNNING and time.monotonic() < deadline:
            time.sleep(min(0.2, deadline - time.monotonic()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--candles", type=int, default=240)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    stream(
        Path(args.root).resolve(),
        max(1.0, min(10.0, args.interval)),
        _text(args.timeframe, 8) or "1m",
        max(60, min(500, args.candles)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
