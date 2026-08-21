#!/usr/bin/env python3
"""Emit read-only bot/exchange snapshots as newline-delimited JSON.

The Windows Rust monitor launches this script through SSH.  It intentionally
contains no order creation, cancellation, leverage, or configuration mutation.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import signal
import time
from pathlib import Path

import ccxt

from options_trading.client import BinanceOptionsClient
from options_trading.config import normalize_options_config


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
    effective_leverage = notional / margin if margin else leverage
    if leverage <= 1.0 and effective_leverage > 1.05:
        rounded = round(effective_leverage)
        leverage = (
            float(rounded)
            if abs(effective_leverage - rounded) <= 0.15
            else effective_leverage
        )
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


def _rows(payload):
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _first_matching_row(payload, symbol):
    rows = _rows(payload)
    for row in rows:
        if _text(row.get("symbol"), 80) == symbol:
            return row
    return rows[0] if rows else {}


def _option_quantity(raw):
    raw = raw if isinstance(raw, dict) else {}
    quantity = _float(raw.get("quantity"), _float(raw.get("positionAmt"), 0.0))
    return quantity or 0.0


def _option_trailing_floor(entry_price, peak_price):
    if entry_price <= 0 or peak_price <= entry_price:
        return None
    peak_return = peak_price / entry_price - 1.0
    for activation, drawdown, locked_return in (
        (2.00, 0.30, 0.80),
        (1.00, 0.35, 0.35),
        (0.50, 0.40, 0.10),
    ):
        if peak_return >= activation:
            return max(
                peak_price * (1.0 - drawdown),
                entry_price * (1.0 + locked_return),
            )
    return None


def normalize_option_position(
    raw,
    *,
    tracked=None,
    mark=None,
    options_config=None,
    exchange_verified=True,
):
    raw = raw if isinstance(raw, dict) else {}
    tracked = tracked if isinstance(tracked, dict) else {}
    mark = mark if isinstance(mark, dict) else {}
    cfg = options_config if isinstance(options_config, dict) else normalize_options_config({})
    symbol = _text(raw.get("symbol") or tracked.get("symbol"), 80).upper()
    quantity = _option_quantity(raw)
    if abs(quantity) <= 1e-12:
        quantity = _float(tracked.get("quantity"), 0.0) or 0.0
    if not symbol or abs(quantity) <= 1e-12:
        return None
    raw_side = _text(raw.get("side"), 12).upper()
    position_side = raw_side if raw_side in {"LONG", "SHORT"} else (
        "SHORT" if quantity < 0 else "LONG"
    )
    quantity = abs(quantity)
    option_type = _text(tracked.get("side"), 12).upper()
    if option_type not in {"CALL", "PUT"}:
        option_type = "PUT" if symbol.endswith("-P") else "CALL" if symbol.endswith("-C") else "OPTION"
    entry_price = _float(
        raw.get("entryPrice"),
        _float(raw.get("avgPrice"), _float(tracked.get("entry_price"), 0.0)),
    ) or 0.0
    mark_price = _float(
        mark.get("markPrice"),
        _float(raw.get("markPrice"), _float(tracked.get("last_mark"), entry_price)),
    ) or 0.0
    unit = _float(tracked.get("unit"), 1.0) or 1.0
    direction = -1.0 if position_side == "SHORT" else 1.0
    pnl = (mark_price - entry_price) * quantity * unit * direction
    return_percent = (
        (mark_price / entry_price - 1.0) * 100.0 * direction
        if entry_price > 0 and mark_price > 0
        else None
    )
    is_bot = _text(tracked.get("symbol"), 80).upper() == symbol
    entry_cost = _float(tracked.get("entry_total_usdt")) if is_bot else None
    if not entry_cost:
        entry_cost = entry_price * quantity * unit
    expiry_ms = int(_float(tracked.get("expiry_date_ms"), 0.0) or 0)
    dte_days = _float(tracked.get("last_dte_days"))
    if dte_days is None and expiry_ms > 0:
        dte_days = max(0.0, (expiry_ms - int(time.time() * 1000)) / 86_400_000.0)
    peak_mark = _float(tracked.get("peak_mark")) if is_bot else None
    hard_stop = None
    hard_target = None
    trailing_floor = None
    if is_bot and position_side == "LONG" and entry_price > 0:
        hard_stop = entry_price * (1.0 - _float(cfg.get("stop_loss_pct"), 0.55))
        hard_target = entry_price * (1.0 + _float(cfg.get("take_profit_pct"), 3.00))
        trailing_floor = _option_trailing_floor(entry_price, peak_mark or entry_price)
    return {
        "symbol": symbol,
        "underlying": _text(tracked.get("underlying"), 40).upper(),
        "option_type": option_type,
        "position_side": position_side,
        "quantity": quantity,
        "entry_price": entry_price,
        "mark_price": mark_price,
        "entry_cost_usdt": entry_cost,
        "premium_value_usdt": mark_price * quantity * unit,
        "unrealized_pnl": pnl,
        "return_percent": return_percent,
        "source": "BOT" if is_bot else "MANUAL / UNKNOWN",
        "strategy": _text(tracked.get("signal_strategy"), 80) or None if is_bot else None,
        "expiry_date_ms": expiry_ms,
        "dte_days": dte_days,
        "peak_mark": peak_mark,
        "mark_iv": _float(mark.get("markIV"), _float(tracked.get("last_mark_iv"))),
        "delta": _float(mark.get("delta"), _float(tracked.get("last_delta"), _float(tracked.get("entry_delta")))),
        "gamma": _float(mark.get("gamma"), _float(tracked.get("entry_gamma"))),
        "theta": _float(mark.get("theta"), _float(tracked.get("last_theta"), _float(tracked.get("entry_theta")))),
        "vega": _float(mark.get("vega"), _float(tracked.get("entry_vega"))),
        "hard_stop_price": hard_stop,
        "hard_target_price": hard_target,
        "trailing_floor": trailing_floor,
        "exchange_verified": bool(exchange_verified),
    }


def _merge_candles(existing, incoming, limit):
    merged = {int(row.get("ts") or 0): dict(row) for row in existing if row.get("ts")}
    for row in incoming:
        ts = int(row.get("ts") or 0)
        if ts:
            merged[ts] = dict(row)
    return [merged[ts] for ts in sorted(merged)[-limit:]]


def _apply_option_mark_candle(candles, mark_price, now_ms=None, limit=300):
    rows = [dict(row) for row in candles]
    if not mark_price or mark_price <= 0:
        return rows[-limit:]
    now_ms = int(now_ms or time.time() * 1000)
    bucket = now_ms - (now_ms % 60_000)
    if rows and int(rows[-1].get("ts") or 0) == bucket:
        row = rows[-1]
        row["high"] = max(_float(row.get("high"), mark_price), mark_price)
        row["low"] = min(_float(row.get("low"), mark_price), mark_price)
        row["close"] = mark_price
    else:
        opening = _float(rows[-1].get("close"), mark_price) if rows else mark_price
        rows.append(
            {
                "ts": bucket,
                "open": opening,
                "high": max(opening, mark_price),
                "low": min(opening, mark_price),
                "close": mark_price,
                "volume": 0.0,
            }
        )
    return rows[-limit:]


class OptionsMonitorSource:
    """Rate-bounded, read-only Options EAPI source for the desktop monitor."""

    def __init__(self, root, config, timeframe, candle_limit):
        self.root = root
        self.timeframe = timeframe
        self.candle_limit = candle_limit
        self.state_path = root / "runtime" / "options_trading_state.json"
        self.config = normalize_options_config(config.get("options_trading", {}) or {})
        creds = _credentials(config, "binance_mainnet")
        self.client = BinanceOptionsClient(
            creds.get("api_key", ""),
            creds.get("secret_key", ""),
            timeout=self.config.get("request_timeout_seconds", 10),
            request_limit_per_minute=120,
        )
        self.positions = []
        self.positions_verified = False
        self.mark = {}
        self.candles = []
        self.selected_symbol = ""
        self.positions_at = 0.0
        self.mark_at = 0.0
        self.klines_at = 0.0

    def snapshot(self):
        live_config = _load_json(self.root / "config.json", {})
        if isinstance(live_config, dict):
            self.config = normalize_options_config(
                live_config.get("options_trading", {}) or {}
            )
        state = _load_json(self.state_path, {})
        tracked = state.get("active_position") if isinstance(state, dict) else None
        tracked = tracked if isinstance(tracked, dict) else {}
        errors = []
        now = time.monotonic()
        if now - self.positions_at >= 10.0:
            self.positions_at = now
            try:
                self.positions = [
                    row
                    for row in _rows(self.client.positions())
                    if abs(_option_quantity(row)) > 1e-12
                ]
                self.positions_verified = True
            except Exception as exc:
                errors.append(_text(f"OPTIONS_POSITION_READ: {type(exc).__name__}: {exc}", 300))

        tracked_symbol = _text(tracked.get("symbol"), 80).upper()
        selected_symbol = tracked_symbol
        if not selected_symbol and self.positions:
            selected_symbol = _text(self.positions[0].get("symbol"), 80).upper()
        symbol_changed = selected_symbol != self.selected_symbol
        if symbol_changed:
            self.selected_symbol = selected_symbol
            self.mark = {}
            self.candles = []
            self.mark_at = 0.0
            self.klines_at = 0.0

        emit_history = symbol_changed
        if selected_symbol and now - self.mark_at >= 5.0:
            self.mark_at = now
            try:
                self.mark = _first_matching_row(
                    self.client.mark_price(selected_symbol), selected_symbol
                )
            except Exception as exc:
                errors.append(_text(f"OPTIONS_MARK_READ: {type(exc).__name__}: {exc}", 300))
        if selected_symbol and now - self.klines_at >= 10.0:
            self.klines_at = now
            try:
                history = normalize_candles(
                    self.client.klines(
                        selected_symbol,
                        interval=self.timeframe,
                        limit=self.candle_limit,
                    )
                )
                self.candles = _merge_candles(
                    self.candles, history, self.candle_limit
                )
                emit_history = True
            except Exception as exc:
                errors.append(_text(f"OPTIONS_KLINE_READ: {type(exc).__name__}: {exc}", 300))

        selected_mark = _float(self.mark.get("markPrice"))
        if selected_mark is None:
            for raw in self.positions:
                if _text(raw.get("symbol"), 80).upper() == selected_symbol:
                    selected_mark = _float(raw.get("markPrice"))
                    break
        if selected_mark is None:
            selected_mark = _float(tracked.get("last_mark"))
        live_candles = _apply_option_mark_candle(
            self.candles,
            selected_mark,
            limit=self.candle_limit,
        )
        self.candles = live_candles

        positions = []
        matched_tracked = False
        for raw in self.positions:
            symbol = _text(raw.get("symbol"), 80).upper()
            position_tracked = tracked if symbol == tracked_symbol else None
            matched_tracked = matched_tracked or bool(position_tracked)
            mark = self.mark if symbol == selected_symbol else raw
            normalized = normalize_option_position(
                raw,
                tracked=position_tracked,
                mark=mark,
                options_config=self.config,
                exchange_verified=self.positions_verified,
            )
            if normalized:
                positions.append(normalized)
        if tracked_symbol and not matched_tracked:
            normalized = normalize_option_position(
                {},
                tracked=tracked,
                mark=self.mark,
                options_config=self.config,
                exchange_verified=False,
            )
            if normalized:
                positions.insert(0, normalized)

        state_error = _text(state.get("last_error"), 300) if isinstance(state, dict) else ""
        if state_error:
            errors.append(state_error)
        return {
            "enabled": bool(self.config.get("enabled")),
            "exchange_mode": "binance_mainnet",
            "selected_symbol": selected_symbol or None,
            "timeframe": self.timeframe,
            "positions": positions,
            "candles": live_candles if emit_history else [],
            "candle": live_candles[-1] if live_candles else None,
            "cash_bankroll_usdt": _float(state.get("cash_bankroll_usdt")) if isinstance(state, dict) else None,
            "capital_limit_usdt": _float(state.get("capital_limit_usdt"), self.config.get("capital_limit_usdt")) if isinstance(state, dict) else self.config.get("capital_limit_usdt"),
            "last_reason": _text(state.get("last_reason"), 500) if isinstance(state, dict) else "",
            "state_updated_at": _text(state.get("updated_at"), 80) if isinstance(state, dict) else "",
            "last_manage_success_ts": _float(state.get("last_manage_success_ts"), 0.0) if isinstance(state, dict) else 0.0,
            "manage_error_streak": int(_float(state.get("manage_error_streak"), 0.0) or 0) if isinstance(state, dict) else 0,
            "error": " | ".join(dict.fromkeys(errors)) or None,
        }


def _fallback_symbol(runtime):
    if isinstance(runtime, dict):
        for row in runtime.get("status_rows", []):
            if not isinstance(row, dict):
                continue
            symbol = _canonical_symbol(row.get("symbol") or row.get("key"))
            if symbol and "SCANN" not in symbol.upper() and symbol.upper() != "PAUSED":
                return symbol
        bot = runtime.get("bot") if isinstance(runtime.get("bot"), dict) else {}
        for value in (bot.get("current_symbol"), bot.get("scanner_active_symbol")):
            symbol = _canonical_symbol(value)
            if symbol:
                return symbol
    return "BTC/USDT"


def _emit(payload):
    try:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
    except BrokenPipeError:
        os._exit(0)


def stream(root, interval, timeframe, candle_limit):
    config = _load_json(root / "config.json", {})
    if not config:
        raise RuntimeError("config.json could not be loaded")
    mode, private, public = build_exchanges(config)
    private.load_markets()
    public.load_markets()
    options_source = OptionsMonitorSource(root, config, timeframe, candle_limit)
    options_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="option-monitor")
    options_future = None
    options_snapshot = {
        "enabled": bool(options_source.config.get("enabled")),
        "exchange_mode": "binance_mainnet",
        "timeframe": timeframe,
        "last_reason": "옵션 상태 연결 중",
    }
    runtime_path = root / "runtime" / "desktop_monitor.json"
    last_symbol = None
    last_runtime_epoch = 0

    while RUNNING:
        started = time.monotonic()
        runtime = _load_json(runtime_path, {})
        if options_future is not None and options_future.done():
            try:
                options_snapshot = options_future.result()
            except Exception as exc:
                options_snapshot = dict(options_snapshot)
                options_snapshot["error"] = _text(
                    f"OPTIONS_MONITOR_READ: {type(exc).__name__}: {exc}", 500
                )
            options_future = None
        if options_future is None:
            options_future = options_executor.submit(options_source.snapshot)
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
                "options": options_snapshot,
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
                    "options": options_snapshot,
                    "error": _text(f"{type(exc).__name__}: {exc}", 500),
                }
            )
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + max(0.2, interval - elapsed)
        while RUNNING and time.monotonic() < deadline:
            time.sleep(min(0.2, deadline - time.monotonic()))
    options_executor.shutdown(wait=False)


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
