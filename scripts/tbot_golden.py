#!/usr/bin/env python3
"""Golden dump for the Rust port: runs the original Python functions and
stores each result as JSON.  Usage:
    python scripts/tbot_golden.py tbot/tests/golden.json
"""
from __future__ import annotations

import json
import random
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from btc_ema_pullback import rules as R
from btc_ema_pullback import signals as S
from btc_ema_pullback.config import default_btc_pullback_config
from btc_ma_cross import signals as SC
from btc_ma_cross.config import default_btc_ma_cross_config
from trading_safety import sma_periods as P


def _mk_candles(rng, n, start_ts, step_ms, start_price, flat_every=None):
    rows = []
    price = start_price
    for i in range(n):
        ts = start_ts + i * step_ms
        flat = flat_every is not None and (i // flat_every) % 2 == 1
        if flat:
            o = c = round(price, 2)
            h = o
            l = o
        else:
            drift = rng.uniform(-0.01, 0.012)
            o = price
            c = max(1.0, o * (1.0 + drift))
            hi = max(o, c) * (1.0 + rng.uniform(0.0, 0.005))
            lo = min(o, c) * (1.0 - rng.uniform(0.0, 0.005))
            h, l = round(hi, 2), round(lo, 2)
        v = 0.0 if flat else round(rng.uniform(1.0, 100.0), 3)
        rows.append([float(ts), round(o, 2), round(h, 2), round(l, 2), round(c, 2), v])
        price = c
    return rows


def _py_to_json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _py_to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_py_to_json(v) for v in value]
    return value


def _freeze_cross(cfg, fast, slow, tf):
    cfg = dict(cfg)
    cfg["fast_period"] = fast
    cfg["slow_period"] = slow
    cfg["timeframe"] = tf
    return cfg


def main(out_path):
    rng = random.Random(20240101)
    base_cfg = default_btc_ma_cross_config()
    pull_cfg = default_btc_pullback_config()
    cases = []

    def emit(fn, args, expect):
        cases.append({"fn": fn, "args": args, "expect": _py_to_json(expect)})

    # --- candles / sma ---
    sets = []
    for n, tf in [(50, "3m"), (250, "5m"), (400, "15m"), (250, "1h")]:
        step = S.TIMEFRAME_MS[tf]
        rows = _mk_candles(rng, n, 1_700_000_000_000, step, 30000.0)
        sets.append((rows, tf))
    flat_rows = _mk_candles(rng, 400, 1_700_000_000_000, 900_000, 30000.0, flat_every=8)
    sets.append((flat_rows, "15m"))

    for rows, tf in sets:
        last_ts = rows[-1][0]
        step = S.TIMEFRAME_MS[tf]
        for now in (int(last_ts + step + 1000), int(last_ts + step + step * 50)):
            emit("closed_candles", {"rows": rows, "timeframe": tf, "now_ms": now},
                 S.closed_candles(rows, tf, now))

    for period in (3, 5, 20, 200):
        closes = [r[4] for r in sets[2][0]]
        emit("sma", {"values": closes, "period": period}, SC.sma(closes, period))

    # --- evaluate_cross with real crosses ---
    cross_cfgs = [(3, 200, "1h"), (1, 200, "15m"), (5, 20, "5m"), (9, 21, "1h")]
    rows_1h = sets[3][0]
    rows_15m = sets[2][0]
    rows_5m = sets[1][0]
    for fast, slow, tf in cross_cfgs:
        rows = {"1h": rows_1h, "15m": rows_15m, "5m": rows_5m}[tf]
        step = S.TIMEFRAME_MS[tf]
        cfg = _freeze_cross(base_cfg, fast, slow, tf)
        last_ts = rows[-1][0]
        for now in (int(last_ts + step + 1000), int(last_ts + step + step * 5)):
            emit("evaluate_cross", {
                "rows": rows, "cfg": cfg, "now_ms": now, "symbol": "BTCUSDT",
                "ccxt_symbol": None,
            }, SC.evaluate_cross(rows, cfg, now, "BTCUSDT", None))

    # Force a real cross: alternating blocks on a slow SMA.
    cross_rows = []
    price = 100.0
    for i in range(400):
        block = i // 100
        drift = 0.01 if block % 2 == 0 else -0.01
        price *= (1.0 + drift)
        o = price / (1.0 + drift)
        cross_rows.append([float(1_700_000_000_000 + i * 3_600_000), round(o, 4),
                           round(max(o, price) * 1.001, 4), round(min(o, price) * 0.999, 4),
                           round(price, 4), 10.0])
    cfg = _freeze_cross(base_cfg, 5, 20, "1h")
    last_ts = cross_rows[-1][0]
    emit("evaluate_cross", {
        "rows": cross_rows, "cfg": cfg, "now_ms": int(last_ts + 3_600_000 + 1000),
        "symbol": "BTCUSDT", "ccxt_symbol": None,
    }, SC.evaluate_cross(cross_rows, cfg, int(last_ts + 3_600_000 + 1000), "BTCUSDT", None))

    # Scan truncated random walks until every outcome (fresh LONG/SHORT cross,
    # stale cross, flat market, too few candles) has golden cases.
    wanted = {"LONG": 3, "SHORT": 3, "SIGNAL_STALE": 2, "FLAT_MARKET": 2, "INSUFFICIENT_CANDLES": 2}
    walk_rng = random.Random(7)
    for fast, slow, tf in [(3, 20, "5m"), (1, 50, "3m"), (3, 200, "15m"), (5, 20, "1h")]:
        step = S.TIMEFRAME_MS[tf]
        walk = _mk_candles(walk_rng, slow + 160, 1_700_000_000_000, step, 50.0)
        cfg = _freeze_cross(base_cfg, fast, slow, tf)
        for end in range(slow - 5, len(walk) + 1):
            rows = walk[:end]
            last_ts = rows[-1][0]
            for now in (int(last_ts + step + 1000), int(last_ts + step * 3)):
                got = SC.evaluate_cross(rows, cfg, now, "SOLUSDT", "SOL/USDT:USDT")
                key = got["side"] or got.get("skip_reason", "").split(":")[0]
                if wanted.get(key, 0) > 0:
                    wanted[key] -= 1
                    emit("evaluate_cross", {
                        "rows": rows, "cfg": cfg, "now_ms": now, "symbol": "SOLUSDT",
                        "ccxt_symbol": "SOL/USDT:USDT",
                    }, got)
    # A flat tape is the only reliable way to hit FLAT_MARKET.
    for tf in ("5m", "1h"):
        cfg = _freeze_cross(base_cfg, 3, 20, tf)
        step = S.TIMEFRAME_MS[tf]
        flat_tape = [[float(1_700_000_000_000 + i * step), 100.0, 100.01, 99.99, 100.0, 1.0]
                     for i in range(120)]
        now = int(flat_tape[-1][0] + step + 1000)
        emit("evaluate_cross", {
            "rows": flat_tape, "cfg": cfg, "now_ms": now, "symbol": "BTCUSDT", "ccxt_symbol": None,
        }, SC.evaluate_cross(flat_tape, cfg, now, "BTCUSDT", None))

    closes_flat = [r[4] for r in flat_rows]
    for tf in ("3m", "15m", "1h"):
        cfg = _freeze_cross(base_cfg, 3, 200, tf)
        emit("market_is_flat", {"closed": flat_rows, "cfg": cfg},
             SC.market_is_flat(flat_rows, cfg))

    # --- margin / lock / stop ---
    for side in ("LONG", "SHORT"):
        for entry, price, lev in [(100.0, 100.5, 5.0), (100.0, 95.0, 3.0), (200.0, 210.0, 5.0)]:
            emit("margin_roi", {"side": side, "entry_price": entry, "price": price, "leverage": lev},
                 SC.margin_roi(side, entry, price, lev))
        for best in (100.0, 104.0, 106.5, 112.0, 130.0):
            res = SC.profit_lock_target(side, 100.0, best, 5.0, 5.0, 5.0, 1.0)
            emit("profit_lock_target", {
                "side": side, "entry_price": 100.0, "best_price": best, "leverage": 5.0,
                "start": 5.0, "step": 5.0, "gap": 1.0,
            }, list(res) if res is not None else None)
        for roi in (0, 10, 20, 30, 50):
            emit("emergency_stop_price", {
                "side": side, "entry_price": 100.0, "leverage": 5.0, "roi_percent": float(roi),
            }, SC.emergency_stop_price(side, 100.0, 5.0, float(roi)))

    # --- rules ---
    sym = {
        "symbol": "BTCUSDT",
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10", "maxPrice": "1000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.002", "minQty": "0.002", "maxQty": "500"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }
    emit("parse_trading_rules", {"symbol_info": sym}, R.parse_trading_rules(sym).as_log())

    for value, step in [("123.4567", "0.01"), ("0.5", "1"), ("7.999", "0.001"), ("100", "0.5")]:
        emit("floor_to_step", {"value": value, "step": step}, R.floor_to_step(value, step))
        emit("ceil_to_step", {"value": value, "step": step}, R.ceil_to_step(value, step))

    for price, tick, direction in [
        ("100.567", "0.10", "down"), ("100.567", "0.10", "up"), ("100.567", "0.10", "nearest"),
        ("12345.678", "1", "down"), ("12345.678", "10", "nearest"), ("99.995", "0.05", "nearest"),
    ]:
        emit("round_price_to_tick", {"price": price, "tick": tick, "direction": direction},
             R.round_price_to_tick(price, tick, direction))

    parsed = R.parse_trading_rules(sym)
    for side, entry in [("LONG", "50000"), ("SHORT", "50000"), ("LONG", "0.1234")]:
        stop, take = R.protection_prices(side, entry, pull_cfg, parsed)
        emit("protection_prices", {"side": side, "avg_entry_price": entry, "cfg": pull_cfg},
             {"stop": stop, "take": take})

    risk_cfg = pull_cfg
    for price, qty in [("50000", "0.001"), ("50000", "0.01"), ("100", "10")]:
        emit("estimate_trade_risk", {"price": price, "quantity": qty,
                                     "cfg": risk_cfg, "taker_fee_rate": "0.0005"},
             R.estimate_trade_risk(price, qty, risk_cfg, "0.0005"))

    trading_rules = R.parse_trading_rules(sym)
    order_cases = [
        dict(price="50000", wallet_balance="100", available_balance="100",
             sizing_mode="fixed", taker_fee_rate="0.0005"),
        dict(price="50000", wallet_balance="100", available_balance="100",
             sizing_mode="risk_based", taker_fee_rate="0.0005"),
        dict(price="0", wallet_balance="100", available_balance="100",
             sizing_mode="fixed", taker_fee_rate="0.0005"),
        dict(price="50000", wallet_balance="0", available_balance="100",
             sizing_mode="fixed", taker_fee_rate="0.0005"),
        dict(price="50000", wallet_balance="100", available_balance="0.01",
             sizing_mode="fixed", taker_fee_rate="0.0005"),
    ]
    for oc in order_cases:
        emit("validate_and_normalize_order_quantity", {
            "price": oc["price"], "wallet_balance": oc["wallet_balance"],
            "available_balance": oc["available_balance"], "cfg": risk_cfg,
            "taker_fee_rate": oc["taker_fee_rate"], "sizing_mode": oc["sizing_mode"],
        }, R.validate_and_normalize_order_quantity(
            rules=trading_rules, cfg=risk_cfg, **oc))

    # Error cases for rules.
    def emit_err(fn, args, call):
        try:
            call()
            raise RuntimeError("expected error")
        except Exception as exc:  # noqa: BLE001
            emit(fn, args, {"error": str(exc)})

    emit_err("parse_trading_rules", {"symbol_info": {}}, lambda: R.parse_trading_rules({}))
    emit_err("parse_trading_rules",
             {"symbol_info": {**sym, "filters": []}},
             lambda: R.parse_trading_rules({**sym, "filters": []}))
    emit_err("floor_to_step", {"value": "1", "step": "0"}, lambda: R.floor_to_step("1", "0"))
    emit_err("round_price_to_tick", {"price": "1", "tick": "0", "direction": "down"},
             lambda: R.round_price_to_tick("1", "0", "down"))

    # --- periods ---
    for fast, slow in [(3, 200), (1, 200), (5, 60), (9, 21)]:
        emit("validate_periods", {"fast": fast, "slow": slow}, list(P.validate_periods(fast, slow)))
    for fast, slow in [(3, 3), (0, 200), (501, 600), (200, 3), (3.5, 200)]:
        try:
            P.validate_periods(fast, slow)
            raise RuntimeError("expected error")
        except P.PeriodError as exc:
            emit("validate_periods", {"fast": fast, "slow": slow}, {"error": str(exc)})

    for args in (["1", "200"], ["1/200"], ["9,21"], ["3"], ["1", "2", "3"], []):
        try:
            emit("parse_period_command", {"args": list(args)}, list(P.parse_period_command(list(args))))
        except P.PeriodError as exc:
            emit("parse_period_command", {"args": list(args)}, {"error": str(exc)})

    for fast, slow in [(3, 120), (3, 200), (1, 200), (9, 21)]:
        emit("periods_label", {"fast": fast, "slow": slow}, P.periods_label(fast, slow))
    for slow in (50, 200, 500):
        emit("required_candle_history", {"slow_period": slow}, P.required_candle_history(slow))

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"cases": cases}, fh, indent=None)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/tbot_golden.py OUT.json", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
