#!/usr/bin/env python
"""Baseline UTBot ATR/RR backtest harness.

This is intentionally conservative and small. It gives us a reproducible
baseline before promoting more live UT Breakout logic into pure modules.

CSV columns: timestamp, open, high, low, close, volume
"""

import argparse
import csv
import json
import math
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utbreakout.risk import calculate_risk_plan


def _float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def load_ohlcv_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rows.append({
                "timestamp": row.get("timestamp") or row.get("time") or "",
                "open": _float(row.get("open")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "close": _float(row.get("close")),
                "volume": _float(row.get("volume")),
            })
    return [row for row in rows if row["open"] > 0 and row["high"] > 0 and row["low"] > 0 and row["close"] > 0]


def wilder_atr(rows, period):
    tr = []
    for idx, row in enumerate(rows):
        prev_close = rows[idx - 1]["close"] if idx > 0 else row["close"]
        tr.append(max(
            abs(row["high"] - row["low"]),
            abs(row["high"] - prev_close),
            abs(row["low"] - prev_close),
        ))
    atr = [None] * len(rows)
    if len(rows) < period:
        return atr
    atr[period - 1] = sum(tr[:period]) / period
    for idx in range(period, len(rows)):
        atr[idx] = ((atr[idx - 1] * (period - 1)) + tr[idx]) / period
    return atr


def utbot_rows(rows, key_value=2.0, atr_period=10):
    atr = wilder_atr(rows, atr_period)
    trail = [None] * len(rows)
    signal = [None] * len(rows)
    bias = [None] * len(rows)
    for idx, row in enumerate(rows):
        if atr[idx] is None:
            continue
        src = row["close"]
        nloss = atr[idx] * key_value
        if idx == 0 or trail[idx - 1] is None:
            trail[idx] = src - nloss
        else:
            prev_stop = trail[idx - 1]
            prev_src = rows[idx - 1]["close"]
            if src > prev_stop and prev_src > prev_stop:
                trail[idx] = max(prev_stop, src - nloss)
            elif src < prev_stop and prev_src < prev_stop:
                trail[idx] = min(prev_stop, src + nloss)
            elif src > prev_stop:
                trail[idx] = src - nloss
            else:
                trail[idx] = src + nloss
            if src > trail[idx] and prev_src <= prev_stop:
                signal[idx] = "long"
            elif src < trail[idx] and prev_src >= prev_stop:
                signal[idx] = "short"
        bias[idx] = "long" if src > trail[idx] else "short" if src < trail[idx] else None
    return atr, trail, signal, bias


def _apply_slippage(price, side, bps, is_entry):
    direction = 1.0
    if side == "short":
        direction = -1.0
    if not is_entry:
        direction *= -1.0
    return price * (1.0 + direction * (bps / 10000.0))


def simulate_utbot_rr(
    rows,
    *,
    key_value,
    atr_period,
    initial_balance=1000.0,
    risk_per_trade_percent=1.0,
    max_risk_per_trade_usdt=10.0,
    leverage=5.0,
    stop_atr_multiplier=1.5,
    rr_multiple=2.0,
    fee_bps=4.0,
    slippage_bps=1.0,
):
    atr, trail, signal, _ = utbot_rows(rows, key_value=key_value, atr_period=atr_period)
    balance = float(initial_balance)
    equity_peak = balance
    max_drawdown = 0.0
    trades = []
    position = None

    for idx in range(len(rows)):
        row = rows[idx]
        if position:
            side = position["side"]
            stop_hit = row["low"] <= position["stop_loss"] if side == "long" else row["high"] >= position["stop_loss"]
            target_hit = row["high"] >= position["take_profit"] if side == "long" else row["low"] <= position["take_profit"]
            exit_reason = None
            exit_price = None
            if stop_hit:
                exit_reason = "SL"
                exit_price = position["stop_loss"]
            elif target_hit:
                exit_reason = "TP"
                exit_price = position["take_profit"]
            if exit_reason:
                exit_price = _apply_slippage(exit_price, side, slippage_bps, is_entry=False)
                qty = position["qty"]
                gross = (exit_price - position["entry_price"]) * qty
                if side == "short":
                    gross *= -1.0
                fees = (position["entry_price"] * qty + exit_price * qty) * fee_bps / 10000.0
                pnl = gross - fees
                balance += pnl
                trade = dict(position)
                trade.update({
                    "exit_idx": idx,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "pnl": pnl,
                    "balance": balance,
                })
                trades.append(trade)
                equity_peak = max(equity_peak, balance)
                max_drawdown = max(max_drawdown, equity_peak - balance)
                position = None
            continue

        if signal[idx] not in {"long", "short"} or atr[idx] is None:
            continue
        side = signal[idx]
        entry = _apply_slippage(row["close"], side, slippage_bps, is_entry=True)
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry,
                atr_value=atr[idx],
                stop_atr_multiplier=stop_atr_multiplier,
                ut_stop=trail[idx],
                take_profit_r_multiple=rr_multiple,
                min_risk_reward=rr_multiple,
                balance_usdt=balance,
                risk_per_trade_percent=risk_per_trade_percent,
                max_risk_per_trade_usdt=max_risk_per_trade_usdt,
                leverage=leverage,
            )
        except ValueError:
            continue
        position = {
            "entry_idx": idx,
            "side": side,
            "entry_price": entry,
            "qty": plan["qty"],
            "stop_loss": plan["stop_loss"],
            "take_profit": plan["take_profit"],
            "risk_usdt": plan["risk_usdt"],
            "planned_margin": plan["planned_margin"],
            "planned_notional": plan["planned_notional"],
        }

    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    net_pnl = sum(t["pnl"] for t in trades)
    return {
        "trades": len(trades),
        "wins": sum(1 for t in trades if t["pnl"] > 0),
        "losses": sum(1 for t in trades if t["pnl"] <= 0),
        "net_pnl": net_pnl,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "expectancy": net_pnl / len(trades) if trades else None,
        "max_drawdown_usdt": max_drawdown,
        "max_drawdown_pct": max_drawdown / max(initial_balance, 1e-9) * 100.0,
        "ending_balance": balance,
        "trades_detail": trades,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="OHLCV CSV path")
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--max-risk-usdt", type=float, default=10.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    rows = load_ohlcv_csv(args.csv)
    variants = {
        "UTBot_2_10": {"key_value": 2.0, "atr_period": 10},
        "UTBot_2_5_14": {"key_value": 2.5, "atr_period": 14},
    }
    report = {}
    for name, params in variants.items():
        result = simulate_utbot_rr(
            rows,
            initial_balance=args.initial_balance,
            risk_per_trade_percent=args.risk_pct,
            max_risk_per_trade_usdt=args.max_risk_usdt,
            leverage=args.leverage,
            fee_bps=args.fee_bps,
            slippage_bps=args.slippage_bps,
            **params,
        )
        result.pop("trades_detail", None)
        report[name] = result

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"CSV: {Path(args.csv)} / candles: {len(rows)}")
        for name, result in report.items():
            pf = result["profit_factor"]
            exp = result["expectancy"]
            pf_text = f"{pf:.2f}" if pf is not None else "n/a"
            exp_text = f"{exp:.4f}" if exp is not None else "n/a"
            print(
                f"{name}: trades={result['trades']} net={result['net_pnl']:.2f} "
                f"PF={pf_text} "
                f"EXP={exp_text} "
                f"MDD={result['max_drawdown_pct']:.2f}%"
            )


if __name__ == "__main__":
    main()
